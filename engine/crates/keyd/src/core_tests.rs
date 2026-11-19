//! The logic against the mocked devices, on a clock the tests move by hand. Most tests end by
//! checking the two things that would leave a keyboard dead: a keyboard still grabbed, and a key
//! still held down on the virtual keyboard.

use std::time::Duration;

use super::*;
use crate::keys::tests::{description, keyboard};
use crate::keys::*;
use crate::mock::{MockDevices, World};

const CLIENT: ConnId = 1;
const OTHER: ConnId = 2;
const PEER: Peer = Peer {
    pid: 4242,
    uid: 1000,
    gid: 100,
};
const SESSION: &str = "2";
const KEY_LEFTCTRL: u16 = 29;
const KEY_T: u16 = 20;

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

struct T {
    core: Core<MockDevices>,
    w: World,
    now: Instant,
    /// Everything the core asked of the loop, oldest first, not yet looked at.
    out: Vec<Output>,
}

impl T {
    fn new() -> T {
        let w = World::new();
        let mut t = T {
            core: Core::new(w.devices()),
            w,
            now: Instant::now(),
            out: Vec::new(),
        };
        t.core.front_changed(front(SESSION, 1000), t.now);
        t
    }

    /// With a keyboard plugged in, a client admitted, capturing, and the keyboard held.
    fn capturing() -> (T, u32) {
        let mut t = T::new();
        let k = t.w.plug("AT Translated Set 2 keyboard");
        t.admit(CLIENT);
        t.send(FromClient::Start);
        t.pass(VIRTUAL_SETTLE);
        assert!(t.w.grabbed(k), "{:?}", t.sent());
        t.out.clear();
        (t, k)
    }

    fn take(&mut self) {
        self.out.extend(self.core.take_output());
    }

    /// Moves the clock on and lets the core act on what fell due, with the client, if there is
    /// one, sending its heartbeat as it should.
    fn pass(&mut self, d: Duration) {
        self.advance(d, true);
    }

    /// Moves the clock on with the client silent.
    fn quiet(&mut self, d: Duration) {
        self.advance(d, false);
    }

    fn advance(&mut self, mut d: Duration, beat: bool) {
        loop {
            if beat && let Some(c) = &self.core.client {
                let conn = c.conn;
                self.from(conn, FromClient::Heartbeat { counter: 0 });
            }
            let step = d.min(HEARTBEAT_INTERVAL);
            self.now += step;
            self.core.tick(self.now);
            self.take();
            d -= step;
            if d.is_zero() {
                return;
            }
        }
    }

    fn connect(&mut self, conn: ConnId, peer: Peer) {
        self.core.connected(conn, peer, self.now);
        self.take();
    }

    fn from(&mut self, conn: ConnId, m: FromClient) {
        self.core.received(conn, &m.encode(), self.now);
        self.take();
    }

    fn send(&mut self, m: FromClient) {
        self.from(CLIENT, m);
    }

    fn hello(&mut self, conn: ConnId) {
        self.from(
            conn,
            FromClient::Hello {
                version: PROTOCOL_VERSION,
                features: 0,
            },
        );
    }

    /// Connects, says HELLO, and is admitted to the session in front.
    fn admit(&mut self, conn: ConnId) {
        self.connect(conn, PEER);
        self.hello(conn);
        assert!(
            self.out.contains(&Output::Admit(conn, PEER)),
            "{:?}",
            self.out
        );
        self.core.admitted(conn, Ok(SESSION.into()), self.now);
        self.take();
        let welcome =
            |o: &Output| matches!(o, Output::Send(c, ToClient::Welcome { .. }) if *c == conn);
        assert!(self.out.iter().any(welcome), "{:?}", self.out);
        self.out.retain(|o| !welcome(o));
    }

    /// What the client was sent since last asked, but the echoes of its heartbeats.
    fn sent(&mut self) -> Vec<ToClient> {
        self.sent_to(CLIENT)
    }

    fn sent_to(&mut self, conn: ConnId) -> Vec<ToClient> {
        self.everything_sent_to(conn)
            .into_iter()
            .filter(|m| !matches!(m, ToClient::HeartbeatEcho { .. }))
            .collect()
    }

    fn everything_sent_to(&mut self, conn: ConnId) -> Vec<ToClient> {
        let (mine, rest): (Vec<Output>, Vec<Output>) = std::mem::take(&mut self.out)
            .into_iter()
            .partition(|o| matches!(o, Output::Send(c, _) if *c == conn));
        self.out = rest;
        mine.into_iter()
            .map(|o| match o {
                Output::Send(_, m) => m,
                _ => unreachable!(),
            })
            .collect()
    }

    fn closed(&self, conn: ConnId) -> bool {
        self.out.contains(&Output::Close(conn))
    }

    fn press(&mut self, k: u32, code: u16) {
        self.w.press(k, code);
        self.core.keyboard_ready(k, self.now);
        self.take();
    }

    fn release(&mut self, k: u32, code: u16) {
        self.w.release(k, code);
        self.core.keyboard_ready(k, self.now);
        self.take();
    }

    fn verdict(&mut self, seq: u64, keep: bool) {
        self.send(FromClient::Verdict { seq, keep });
    }

    /// The keys the client was sent since last asked, with everything else it was sent dropped.
    fn keys(&mut self) -> Vec<Key> {
        self.sent()
            .into_iter()
            .filter_map(|m| match m {
                ToClient::Key(k) => Some(k),
                _ => None,
            })
            .collect()
    }

    /// The sequence number of the last key the client was sent.
    fn last_seq(&mut self) -> u64 {
        self.keys().last().expect("a key was sent").seq
    }

    fn assert_let_go(&self) {
        assert!(!self.w.any_grabbed(), "a keyboard is still grabbed");
        assert!(
            self.w.virtual_down().is_empty(),
            "keys still down on the virtual keyboard: {:?}",
            self.w.virtual_down()
        );
        assert!(!self.core.holding());
    }
}

fn front(id: &str, uid: u32) -> Front {
    on_console(id, uid, X_CONSOLE)
}

/// The console an X session is on, by logind's word, unless a test says otherwise.
const X_CONSOLE: u32 = 7;

fn on_console(id: &str, uid: u32, vt: u32) -> Front {
    Front::Session {
        id: id.into(),
        uid,
        vt,
    }
}

fn released(reason: Released) -> ToClient {
    ToClient::Released { reason }
}

fn goodbye_reason(m: &[ToClient]) -> Option<Goodbye> {
    m.iter().find_map(|m| match m {
        ToClient::Goodbye { reason, .. } => Some(*reason),
        _ => None,
    })
}

fn status(m: &[ToClient], device: u32) -> Vec<DeviceStatus> {
    m.iter()
        .filter_map(|m| match m {
            ToClient::Device {
                device: d, status, ..
            } if *d == device => Some(*status),
            _ => None,
        })
        .collect()
}

// The start order.

#[test]
fn nothing_is_grabbed_until_the_virtual_keyboard_has_appeared_and_settled() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.admit(CLIENT);
    assert!(!t.w.virtual_exists(), "made before anyone asked");
    t.send(FromClient::Start);
    assert!(t.w.virtual_exists());
    assert!(!t.w.is_open(k) && !t.w.grabbed(k));
    t.pass(VIRTUAL_SETTLE - ms(1));
    assert!(!t.w.is_open(k) && !t.w.grabbed(k));
    t.pass(ms(1));
    assert!(t.w.grabbed(k));
    assert_eq!(status(&t.sent(), k), vec![DeviceStatus::Held]);
    assert_eq!(t.w.virtual_made(), 1);
}

#[test]
fn a_virtual_keyboard_that_cannot_be_made_means_nothing_is_grabbed() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.fail_create(true);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    assert_eq!(t.sent(), vec![released(Released::VirtualKeyboard)]);
    t.pass(VIRTUAL_APPEAR_TIMEOUT * 2);
    assert!(!t.w.is_open(k) && !t.w.grabbed(k));
    // It stays that way until the client asks again, which makes it try again.
    t.w.fail_create(false);
    t.pass(VIRTUAL_SETTLE);
    assert!(!t.w.virtual_exists());
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert!(t.w.grabbed(k));
}

#[test]
fn a_virtual_keyboard_that_never_appears_means_nothing_is_grabbed() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.never_appears(true);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_APPEAR_TIMEOUT - ms(1));
    assert!(t.sent().is_empty());
    assert!(!t.w.is_open(k));
    t.pass(ms(1));
    assert_eq!(t.sent(), vec![released(Released::VirtualKeyboard)]);
    assert!(
        !t.w.virtual_exists(),
        "the one that never appeared is destroyed"
    );
    t.pass(VIRTUAL_APPEAR_TIMEOUT);
    assert!(!t.w.is_open(k) && !t.w.grabbed(k));
    t.w.never_appears(false);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert!(t.w.grabbed(k));
    assert_eq!(t.w.virtual_made(), 2);
}

#[test]
fn a_keyboard_with_a_key_down_is_grabbed_only_once_every_key_is_up() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    // Enter, held from starting Linda in a terminal.
    t.w.press(k, KEY_ENTER);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert!(t.w.is_open(k) && !t.w.grabbed(k));
    assert_eq!(status(&t.sent(), k), vec![DeviceStatus::Waiting]);
    for _ in 0..10 {
        t.pass(KEYS_UP_RETRY);
    }
    assert!(!t.w.grabbed(k));
    // Its release reaches the desktop, which saw the press, and the helper reads a copy.
    t.release(k, KEY_ENTER);
    assert!(t.w.grabbed(k));
    assert_eq!(status(&t.sent(), k), vec![DeviceStatus::Held]);
    assert!(
        t.keys().is_empty(),
        "the copy of the release went to the client"
    );
}

#[test]
fn a_key_that_goes_down_as_the_grab_is_taken_undoes_the_grab() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.press_at_grab(k, KEY_A);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert_eq!(t.w.grabs(k), 1);
    assert!(!t.w.grabbed(k), "held with a key down");
    t.pass(KEYS_UP_RETRY);
    assert!(!t.w.grabbed(k));
    t.release(k, KEY_A);
    assert!(t.w.grabbed(k));
    assert!(t.keys().is_empty());
}

// The ordinary path.

#[test]
fn a_kept_key_never_reaches_the_virtual_keyboard() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    let keys = t.keys();
    assert_eq!(keys.len(), 1);
    assert_eq!(
        keys[0],
        Key {
            seq: keys[0].seq,
            device: k,
            code: KEY_A,
            value: KEY_PRESS,
            flags: KEY_ASK
        }
    );
    t.verdict(keys[0].seq, true);
    t.release(k, KEY_A);
    let keys = t.keys();
    assert_eq!((keys[0].value, keys[0].flags), (KEY_RELEASE, 0));
    t.pass(VERDICT_TIMEOUT * 3);
    assert!(t.w.emitted().is_empty());
    assert!(t.sent().is_empty(), "nothing expired");
}

#[test]
fn a_passed_key_goes_out_with_its_release() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    let seq = t.last_seq();
    assert!(t.w.emitted().is_empty(), "out before its verdict");
    t.verdict(seq, false);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    t.release(k, KEY_A);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
}

#[test]
fn later_keys_wait_behind_an_undecided_one() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    t.press(k, KEY_A);
    t.release(k, KEY_A);
    t.release(k, KEY_LEFTSHIFT);
    let seqs: Vec<u64> = t.keys().iter().map(|k| k.seq).collect();
    assert_eq!(seqs.len(), 4);
    assert!(seqs.windows(2).all(|w| w[1] == w[0] + 1));
    // The client answers the second first: nothing may overtake the first.
    t.verdict(seqs[1], false);
    assert!(t.w.emitted().is_empty());
    t.verdict(seqs[0], false);
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_LEFTSHIFT, 1),
            (KEY_A, 1),
            (KEY_A, 0),
            (KEY_LEFTSHIFT, 0)
        ]
    );
}

#[test]
fn a_release_follows_its_press_whichever_way_it_went() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    t.press(k, KEY_INSERT);
    let keys = t.keys();
    t.verdict(keys[0].seq, false);
    t.verdict(keys[1].seq, true);
    t.release(k, KEY_INSERT);
    t.release(k, KEY_LEFTSHIFT);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_LEFTSHIFT, 1), (KEY_LEFTSHIFT, 0)],
        "the kept Insert's release went out"
    );
}

#[test]
fn repeats_go_to_the_client_and_never_to_the_virtual_keyboard() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    let seq = t.last_seq();
    t.verdict(seq, false);
    t.w.repeat(k, KEY_A);
    t.w.repeat(k, KEY_A);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let keys = t.keys();
    assert_eq!(keys.len(), 2);
    assert!(keys.iter().all(|k| k.value == KEY_REPEAT && k.flags == 0));
    t.release(k, KEY_A);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
    assert_eq!(t.keys().len(), 1);
    // A repeat with no press seen is not passed to the client either.
    t.w.repeat(k, KEY_T);
    t.core.keyboard_ready(k, t.now);
    t.take();
    assert!(t.keys().is_empty());
}

#[test]
fn a_key_held_on_two_keyboards_goes_up_with_the_last() {
    let (mut t, k) = T::capturing();
    let k2 = t.w.plug("second keyboard");
    t.core.watch_ready(t.now);
    t.take();
    assert!(t.w.grabbed(k2));
    t.press(k, KEY_LEFTSHIFT);
    t.press(k2, KEY_LEFTSHIFT);
    for key in t.keys() {
        t.verdict(key.seq, false);
    }
    t.release(k, KEY_LEFTSHIFT);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTSHIFT, 1)]);
    t.release(k2, KEY_LEFTSHIFT);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTSHIFT, 1), (KEY_LEFTSHIFT, 0)]);
}

#[test]
fn caps_lock_kept_does_not_toggle_and_passed_it_does() {
    let (mut t, k) = T::capturing();
    // The lights the keyboard had when grabbed are the first word on the locks.
    t.desktop_leds(0);
    t.press(k, KEY_CAPSLOCK);
    t.release(k, KEY_CAPSLOCK);
    let keys = t.keys();
    t.verdict(keys[0].seq, true);
    t.pass(VERDICT_TIMEOUT);
    assert!(t.w.emitted().is_empty(), "the lock's owner saw Caps Lock");
    assert_eq!(t.w.leds(k), 0);
    // Pressed twice, as NVDA has it toggle the lock, and passed.
    t.press(k, KEY_CAPSLOCK);
    t.release(k, KEY_CAPSLOCK);
    let keys = t.keys();
    t.verdict(keys[0].seq, false);
    assert_eq!(t.w.emitted(), vec![(KEY_CAPSLOCK, 1), (KEY_CAPSLOCK, 0)]);
    // The X server turns the lock on and says so on the virtual keyboard.
    t.desktop_leds(LOCK_CAPS);
    assert_eq!(t.w.leds(k), LOCK_CAPS);
    assert_eq!(t.sent(), vec![ToClient::Locks { leds: LOCK_CAPS }]);
}

impl T {
    fn desktop_leds(&mut self, leds: u16) {
        self.w.desktop_sets_leds(leds);
        self.core.virtual_ready(self.now);
        self.take();
    }
}

#[test]
fn the_locks_come_from_the_first_keyboard_until_the_desktop_says() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.set_device_leds(k, LOCK_NUM);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(
        sent.contains(&ToClient::Locks { leds: LOCK_NUM }),
        "{sent:?}"
    );
    // A keyboard plugged in later shows them.
    let k2 = t.w.plug("second keyboard");
    t.core.watch_ready(t.now);
    t.take();
    assert_eq!(t.w.leds(k2), LOCK_NUM);
    // A client admitted later is told at once.
    t.core.disconnected(CLIENT, t.now);
    t.take();
    t.admit(OTHER);
    assert_eq!(t.sent_to(OTHER), vec![ToClient::Locks { leds: LOCK_NUM }]);
}

/// What the client was sent but the state of each keyboard.
fn told(t: &mut T) -> Vec<ToClient> {
    let mut sent = t.sent();
    sent.retain(|m| !matches!(m, ToClient::Device { .. }));
    sent
}

#[test]
fn the_lights_are_told_only_of_the_clients_own_session_while_it_is_in_front() {
    let (mut t, _k) = T::capturing();
    t.desktop_leds(LOCK_NUM);
    assert_eq!(told(&mut t), vec![ToClient::Locks { leds: LOCK_NUM }]);
    t.logind(front("5", 1001));
    t.desktop_leds(LOCK_NUM | LOCK_CAPS);
    assert_eq!(told(&mut t), vec![released(Released::Background)]);
    // Back in front, it is not told of the lights the other session left.
    t.logind(front(SESSION, 1000));
    assert!(told(&mut t).is_empty());
    // Its own X server sets them again, and it hears.
    t.desktop_leds(LOCK_NUM);
    assert_eq!(told(&mut t), vec![ToClient::Locks { leds: LOCK_NUM }]);
    // Nor of lights set on a text console of the same user, before or after logind says so.
    t.console(X_CONSOLE);
    t.console(2);
    t.desktop_leds(LOCK_CAPS);
    t.logind(on_console("c2", 1000, 2));
    t.desktop_leds(LOCK_SCROLL);
    assert_eq!(told(&mut t), vec![released(Released::Background)]);
    t.console(X_CONSOLE);
    t.logind(front(SESSION, 1000));
    assert!(told(&mut t).is_empty());
}

#[test]
fn each_client_counts_its_keys_from_one() {
    let (mut t, k) = T::capturing();
    for _ in 0..3 {
        t.press(k, KEY_A);
        let seq = t.last_seq();
        t.verdict(seq, false);
        t.release(k, KEY_A);
    }
    t.core.disconnected(CLIENT, t.now);
    t.take();
    t.out.clear();
    t.admit(OTHER);
    t.from(OTHER, FromClient::Start);
    t.pass(ms(1));
    assert!(t.w.grabbed(k));
    t.press(k, KEY_A);
    let keys: Vec<u64> = t
        .sent_to(OTHER)
        .into_iter()
        .filter_map(|m| match m {
            ToClient::Key(k) => Some(k.seq),
            _ => None,
        })
        .collect();
    assert_eq!(keys, vec![1]);
}

// The bounded wait for a verdict.

#[test]
fn an_unanswered_press_is_passed_on_at_its_bound() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    let seq = t.last_seq();
    assert_eq!(t.core.next_wake(), Some(t.now + VERDICT_TIMEOUT));
    t.pass(VERDICT_TIMEOUT - ms(1));
    assert!(t.w.emitted().is_empty());
    t.pass(ms(1));
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    assert_eq!(t.sent(), vec![ToClient::Expired { seq }]);
    // The verdict that comes late changes nothing, and the release follows the press.
    t.verdict(seq, true);
    t.release(k, KEY_A);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
    assert!(t.core.holding());
}

#[test]
fn each_key_waits_from_its_own_reading_however_many_are_queued() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    t.pass(ms(60));
    t.press(k, KEY_T);
    t.pass(ms(40));
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    t.pass(ms(59));
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    t.pass(ms(1));
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_T, 1)]);
}

#[test]
fn a_client_whose_answers_stop_coming_is_dropped_and_everything_let_go() {
    let (mut t, k) = T::capturing();
    // Control passed and held, then the client's key thread hangs while its heartbeat goes on.
    t.press(k, KEY_LEFTCTRL);
    let seq = t.last_seq();
    t.verdict(seq, false);
    let mut elapsed = Duration::ZERO;
    while !t.closed(CLIENT) {
        t.press(k, KEY_A);
        t.pass(ms(150));
        t.release(k, KEY_A);
        t.send(FromClient::Heartbeat { counter: 0 });
        t.pass(ms(150));
        elapsed += ms(300);
        assert!(elapsed < Duration::from_secs(3), "never dropped");
    }
    assert!(elapsed >= UNANSWERED_SPAN, "dropped after {elapsed:?}");
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Unanswered));
    t.assert_let_go();
    assert!(!t.w.is_open(k));
}

#[test]
fn a_pause_of_the_client_is_not_taken_for_a_hang() {
    let (mut t, k) = T::capturing();
    // Three keys go unanswered over a third of a second, then the client is back.
    for _ in 0..3 {
        t.press(k, KEY_A);
        t.release(k, KEY_A);
        t.pass(ms(110));
    }
    t.press(k, KEY_T);
    let seq = t.last_seq();
    t.verdict(seq, true);
    t.release(k, KEY_T);
    // Another short pause later starts afresh.
    t.pass(Duration::from_secs(1));
    t.send(FromClient::Heartbeat { counter: 1 });
    for _ in 0..3 {
        t.press(k, KEY_A);
        t.release(k, KEY_A);
        t.pass(ms(110));
    }
    assert!(!t.closed(CLIENT));
    assert!(t.w.grabbed(k));
}

#[test]
fn a_press_answered_between_two_unanswered_ones_ends_the_run() {
    let (mut t, k) = T::capturing();
    // A goes unanswered, and B behind it is answered at once, before A's bound runs out.
    t.press(k, KEY_A);
    t.release(k, KEY_A);
    t.press(k, KEY_T);
    let seq = t.last_seq();
    t.verdict(seq, false);
    t.release(k, KEY_T);
    t.pass(ms(110));
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_A, 1), (KEY_A, 0), (KEY_T, 1), (KEY_T, 0)]
    );
    // A second later another press goes unanswered: not a run of a second, since B was answered.
    t.pass(ms(900));
    t.press(k, KEY_A);
    t.release(k, KEY_A);
    t.pass(ms(110));
    assert!(!t.closed(CLIENT), "{:?}", t.sent());
    assert!(t.w.grabbed(k));
}

/// One unanswered press, and another read `apart` after it, also left unanswered.
fn two_unanswered(apart: Duration) -> T {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    t.release(k, KEY_A);
    t.pass(apart);
    t.press(k, KEY_T);
    t.release(k, KEY_T);
    t.pass(VERDICT_TIMEOUT + ms(10));
    t
}

#[test]
fn unanswered_presses_within_the_gap_of_each_other_make_one_run() {
    // The second is read inside the gap, and its bound ends a run spanning more than a second.
    let mut t = two_unanswered(UNANSWERED_GAP - ms(50));
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Unanswered));
    t.assert_let_go();
}

#[test]
fn unanswered_presses_further_apart_than_the_gap_start_a_run_afresh() {
    let mut t = two_unanswered(UNANSWERED_GAP + ms(50));
    assert!(!t.closed(CLIENT), "{:?}", t.sent());
    // However long ago the last one was.
    let mut t = two_unanswered(Duration::from_secs(300));
    assert!(!t.closed(CLIENT), "{:?}", t.sent());
    assert!(t.core.holding());
}

// The heartbeat, and clients that go quiet or go away.

#[test]
fn a_connection_that_never_says_hello_is_turned_away_and_frees_its_place() {
    let mut t = T::new();
    t.connect(OTHER, PEER);
    t.pass(HELLO_TIMEOUT - ms(1));
    assert!(!t.closed(OTHER));
    t.pass(ms(1));
    assert!(t.closed(OTHER));
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::Silent));
    t.out.clear();
    t.admit(CLIENT);
}

#[test]
fn a_client_that_falls_silent_while_holding_a_key_is_dropped_and_the_key_released() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTCTRL);
    let seq = t.last_seq();
    t.verdict(seq, false);
    assert!(t.w.virtual_down().contains(KEY_LEFTCTRL));
    t.quiet(HEARTBEAT_TIMEOUT - ms(1));
    assert!(t.w.grabbed(k));
    t.quiet(ms(1));
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Silent));
    assert!(t.closed(CLIENT));
    t.assert_let_go();
    assert!(!t.w.is_open(k), "the keyboard is closed");
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTCTRL, 1), (KEY_LEFTCTRL, 0)]);
}

#[test]
fn heartbeats_keep_the_client_and_are_echoed() {
    let (mut t, k) = T::capturing();
    for i in 0..20 {
        t.send(FromClient::Heartbeat { counter: i });
        t.quiet(HEARTBEAT_INTERVAL);
    }
    assert!(t.w.grabbed(k));
    let echoes: Vec<ToClient> = t.everything_sent_to(CLIENT);
    assert_eq!(echoes.len(), 20);
    assert_eq!(echoes[19], ToClient::HeartbeatEcho { counter: 19 });
}

#[test]
fn a_client_that_dies_in_the_middle_of_a_chord_leaves_nothing_down() {
    let (mut t, k) = T::capturing();
    // Control passed, the NVDA key kept, and T waiting for its verdict.
    t.press(k, KEY_LEFTCTRL);
    t.press(k, KEY_INSERT);
    let keys = t.keys();
    t.verdict(keys[0].seq, false);
    t.verdict(keys[1].seq, true);
    t.press(k, KEY_T);
    t.core.disconnected(CLIENT, t.now);
    t.take();
    t.assert_let_go();
    assert!(!t.w.is_open(k));
    // T had no verdict and so was passed on, and then released with Control.
    let emitted = t.w.emitted();
    assert_eq!(emitted[..2], [(KEY_LEFTCTRL, 1), (KEY_T, 1)]);
    // The keys still physically down reach the desktop once let go, whose releases it ignores.
    t.release(k, KEY_T);
    t.release(k, KEY_INSERT);
    t.release(k, KEY_LEFTCTRL);
    assert_eq!(t.w.emitted(), emitted);
}

#[test]
fn rubbish_from_the_client_drops_it_and_lets_go() {
    let rubbish: Vec<(&str, Vec<u8>)> = vec![
        ("an unknown type", vec![0x7e]),
        ("a short verdict", vec![VERDICT, 1, 0]),
        ("a verdict that is neither", {
            let mut m = FromClient::Verdict { seq: 1, keep: true }.encode();
            m[9] = 7;
            m
        }),
        (
            "a verdict on a key never sent",
            FromClient::Verdict {
                seq: 1_000_000,
                keep: false,
            }
            .encode(),
        ),
        (
            "a verdict on key 0",
            FromClient::Verdict {
                seq: 0,
                keep: false,
            }
            .encode(),
        ),
        (
            "a second hello",
            FromClient::Hello {
                version: 1,
                features: 0,
            }
            .encode(),
        ),
        ("an empty message", vec![]),
        ("one too long", vec![HEARTBEAT; MAX_MESSAGE + 10]),
    ];
    for (what, m) in rubbish {
        let (mut t, k) = T::capturing();
        t.press(k, KEY_LEFTSHIFT);
        let seq = t.last_seq();
        t.verdict(seq, false);
        t.core.received(CLIENT, &m, t.now);
        t.take();
        assert_eq!(
            goodbye_reason(&t.sent()),
            Some(Goodbye::Malformed),
            "{what}"
        );
        assert!(t.closed(CLIENT), "{what}");
        t.assert_let_go();
    }
}

#[test]
fn a_caller_that_starts_before_it_is_welcome_is_turned_away() {
    let mut t = T::new();
    t.connect(OTHER, PEER);
    t.from(OTHER, FromClient::Start);
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::Malformed));
    assert!(t.closed(OTHER));
    t.out.clear();
    t.connect(OTHER + 1, PEER);
    t.hello(OTHER + 1);
    t.from(OTHER + 1, FromClient::Verdict { seq: 1, keep: true });
    assert!(t.closed(OTHER + 1));
}

// Admission and one client at a time.

#[test]
fn a_second_client_is_turned_away_while_the_first_is_in_front() {
    let (mut t, k) = T::capturing();
    t.connect(OTHER, PEER);
    t.hello(OTHER);
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::Busy));
    assert!(t.closed(OTHER));
    assert!(t.w.grabbed(k));
    // Turned away even when it got as far as admission before the first was admitted.
    let mut t = T::new();
    t.connect(CLIENT, PEER);
    t.hello(CLIENT);
    t.connect(OTHER, PEER);
    t.hello(OTHER);
    t.core.admitted(CLIENT, Ok(SESSION.into()), t.now);
    t.core.admitted(OTHER, Ok(SESSION.into()), t.now);
    t.take();
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::Busy));
    assert!(matches!(t.sent()[0], ToClient::Welcome { .. }));
}

#[test]
fn a_client_of_the_session_in_front_replaces_one_in_the_background() {
    let (mut t, k) = T::capturing();
    // Another user's session comes to the front.
    t.core.front_changed(front("5", 1001), t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    t.assert_let_go();
    let theirs = Peer {
        pid: 777,
        uid: 1001,
        gid: 100,
    };
    t.connect(OTHER, theirs);
    t.hello(OTHER);
    t.core.admitted(OTHER, Ok("5".into()), t.now);
    t.take();
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Replaced));
    assert!(t.closed(CLIENT));
    assert!(matches!(t.sent_to(OTHER)[0], ToClient::Welcome { .. }));
    t.from(OTHER, FromClient::Start);
    t.pass(ms(1));
    assert!(t.w.grabbed(k), "the virtual keyboard is kept");
}

#[test]
fn callers_who_are_not_in_front_are_turned_away() {
    let mut t = T::new();
    t.connect(
        OTHER,
        Peer {
            pid: 1,
            uid: 1001,
            gid: 1,
        },
    );
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::NotInFront));
    t.out.clear();
    // Refusals from logind and polkit are passed on as they come.
    for reason in [
        Goodbye::NotInFront,
        Goodbye::NotAuthorized,
        Goodbye::NoAnswer,
    ] {
        t.connect(OTHER, PEER);
        t.hello(OTHER);
        t.core.admitted(OTHER, Err((reason, "no".into())), t.now);
        t.take();
        assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(reason));
        assert!(t.closed(OTHER));
        t.out.clear();
    }
    // An answer that never comes.
    t.connect(OTHER, PEER);
    t.hello(OTHER);
    t.pass(ADMISSION_TIMEOUT);
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::NoAnswer));
    t.out.clear();
    // The answer that comes after is ignored.
    t.core.admitted(OTHER, Ok(SESSION.into()), t.now);
    t.take();
    assert!(t.sent_to(OTHER).is_empty());
    // Another version.
    t.connect(OTHER, PEER);
    t.from(
        OTHER,
        FromClient::Hello {
            version: 2,
            features: 0,
        },
    );
    let sent = t.sent_to(OTHER);
    assert_eq!(goodbye_reason(&sent), Some(Goodbye::Version));
    assert!(matches!(sent[0], ToClient::Goodbye { version: 1, .. }));
    // No session in front at all.
    t.core.front_changed(Front::Nobody, t.now);
    t.connect(OTHER, PEER);
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::NotInFront));
}

#[test]
fn too_many_callers_at_once_are_turned_away() {
    let mut t = T::new();
    for c in 0..MAX_CALLERS as u64 {
        t.connect(10 + c, PEER);
    }
    assert!(t.out.is_empty());
    t.connect(99, PEER);
    assert_eq!(goodbye_reason(&t.sent_to(99)), Some(Goodbye::TooMany));
}

impl T {
    fn asked(&mut self) -> Vec<ConnId> {
        let (asked, rest): (Vec<Output>, Vec<Output>) = std::mem::take(&mut self.out)
            .into_iter()
            .partition(|o| matches!(o, Output::Admit(..)));
        self.out = rest;
        asked
            .into_iter()
            .map(|o| match o {
                Output::Admit(c, _) => c,
                _ => unreachable!(),
            })
            .collect()
    }
}

#[test]
fn a_question_counts_against_the_callers_waiting_until_its_answer_comes() {
    let mut t = T::new();
    for c in 0..1000 {
        t.connect(100 + c, PEER);
        t.hello(100 + c);
        t.core.disconnected(100 + c, t.now);
    }
    let asked = t.asked();
    assert_eq!(asked.len(), MAX_CALLERS);
    t.out.clear();
    t.connect(OTHER, PEER);
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::TooMany));
    // Turned away for not answering in time, it still counts until the answer comes.
    for c in &asked {
        t.core
            .admitted(*c, Err((Goodbye::NotInFront, "gone".into())), t.now);
    }
    t.take();
    t.out.clear();
    t.admit(CLIENT);
}

#[test]
fn while_the_front_is_unknown_callers_wait_and_nobody_is_asked_about() {
    let mut t = T::new();
    t.logind(Front::Unknown);
    let stranger = Peer {
        pid: 5000,
        uid: 1001,
        gid: 1001,
    };
    t.connect(OTHER, stranger);
    t.hello(OTHER);
    t.connect(CLIENT, PEER);
    t.hello(CLIENT);
    assert!(t.asked().is_empty());
    assert!(t.out.is_empty(), "{:?}", t.out);
    t.logind(front(SESSION, 1000));
    assert_eq!(t.asked(), vec![CLIENT]);
    assert_eq!(goodbye_reason(&t.sent_to(OTHER)), Some(Goodbye::NotInFront));
    t.core.admitted(CLIENT, Ok(SESSION.into()), t.now);
    t.take();
    assert!(matches!(t.sent()[0], ToClient::Welcome { .. }));
    // Not asked at all when logind cannot be reached for the whole of ADMISSION_TIMEOUT.
    t.core.disconnected(CLIENT, t.now);
    t.logind(Front::Unknown);
    t.connect(CLIENT, PEER);
    t.hello(CLIENT);
    t.pass(ADMISSION_TIMEOUT);
    assert!(t.asked().is_empty());
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::NoAnswer));
}

#[test]
fn refusals_are_not_logged_one_line_each() {
    let mut t = T::new();
    let stranger = Peer {
        pid: 5000,
        uid: 1001,
        gid: 1001,
    };
    let before = crate::logged();
    for c in 0..1000 {
        t.connect(100 + c, stranger);
    }
    assert_eq!(crate::logged() - before, REFUSALS_LOGGED);
    assert_eq!(t.core.next_wake(), Some(t.now + REFUSAL_LOG_SPAN));
    // The count of the rest when the span ends, and a line for the first refusal after it.
    t.pass(REFUSAL_LOG_SPAN);
    assert_eq!(crate::logged() - before, REFUSALS_LOGGED + 1);
    t.connect(99, stranger);
    assert_eq!(crate::logged() - before, REFUSALS_LOGGED + 2);
}

#[test]
fn unknown_features_are_not_granted() {
    let mut t = T::new();
    t.connect(CLIENT, PEER);
    t.from(
        CLIENT,
        FromClient::Hello {
            version: 1,
            features: FEATURE_TYPING | 0x8000,
        },
    );
    t.core.admitted(CLIENT, Ok(SESSION.into()), t.now);
    t.take();
    assert!(matches!(t.sent()[0], ToClient::Welcome { features: 0, .. }));
}

// The emergency chord.

#[test]
fn the_emergency_chord_lets_go_at_once_with_the_client_hung() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    let seq = t.last_seq();
    t.verdict(seq, false);
    // From here the client answers nothing.
    t.press(k, KEY_BACKSPACE);
    t.press(k, KEY_ESC);
    t.press(k, KEY_ENTER);
    let sent = t.sent();
    assert!(sent.contains(&released(Released::Emergency)), "{sent:?}");
    assert_eq!(
        sent.iter()
            .filter(|m| matches!(m, ToClient::Key(_)))
            .count(),
        2,
        "Enter went to the client"
    );
    t.assert_let_go();
    assert!(!t.w.is_open(k));
    // The keys waiting were dropped, not passed on: only Shift went out and came back up.
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTSHIFT, 1), (KEY_LEFTSHIFT, 0)]);
    // It stays let go however long the client stays, until it asks again.
    for i in 0..20 {
        t.send(FromClient::Heartbeat { counter: i });
        t.pass(HEARTBEAT_INTERVAL);
    }
    assert!(!t.w.is_open(k));
    for c in CHORD {
        t.w.release(k, c);
    }
    t.w.release(k, KEY_LEFTSHIFT);
    t.send(FromClient::Start);
    assert!(t.w.grabbed(k));
}

#[test]
fn a_key_that_went_down_unseen_goes_nowhere_and_leaves_nothing_down() {
    let (mut t, k) = T::capturing();
    t.w.press(k, KEY_A);
    t.w.overflow(k, &[]);
    t.core.keyboard_ready(k, t.now);
    t.take();
    t.w.release(k, KEY_A);
    t.core.keyboard_ready(k, t.now);
    t.take();
    t.pass(VERDICT_TIMEOUT * 2);
    assert!(t.keys().is_empty());
    assert!(t.w.emitted().is_empty());
    // Pressed again, it is an ordinary key.
    t.press(k, KEY_A);
    assert_eq!(t.keys().len(), 1);
}

#[test]
fn a_chord_held_wholly_while_events_were_lost_lets_go_at_once() {
    let (mut t, k) = T::capturing();
    for c in CHORD {
        t.w.press(k, c);
    }
    t.w.overflow(k, &[]);
    t.core.keyboard_ready(k, t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::Emergency)]);
    t.assert_let_go();
}

#[test]
fn the_emergency_chord_counts_across_keyboards() {
    let (mut t, k) = T::capturing();
    let k2 = t.w.plug("second keyboard");
    t.core.watch_ready(t.now);
    t.take();
    t.press(k, KEY_ESC);
    t.press(k2, KEY_BACKSPACE);
    t.press(k2, KEY_ENTER);
    assert!(t.sent().contains(&released(Released::Emergency)));
    t.assert_let_go();
}

#[test]
fn the_chord_is_not_set_off_by_its_keys_one_after_another() {
    let (mut t, k) = T::capturing();
    for c in CHORD {
        t.press(k, c);
        t.release(k, c);
    }
    t.press(k, KEY_ESC);
    t.press(k, KEY_ENTER);
    t.release(k, KEY_ESC);
    t.press(k, KEY_BACKSPACE);
    assert!(t.w.grabbed(k));
    assert!(!t.sent().contains(&released(Released::Emergency)));
}

// Keyboards that vanish, and lost events.

#[test]
fn a_keyboard_that_vanishes_in_the_middle_of_a_chord_has_its_keys_released() {
    let (mut t, k) = T::capturing();
    let k2 = t.w.plug("second keyboard");
    t.core.watch_ready(t.now);
    t.take();
    t.press(k, KEY_LEFTSHIFT);
    let seq = t.last_seq();
    t.verdict(seq, false);
    // A waits for its verdict as the keyboard goes.
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.w.unplug(k);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let sent = t.sent();
    let releases: Vec<Key> = sent
        .iter()
        .filter_map(|m| match m {
            ToClient::Key(k) => Some(*k),
            _ => None,
        })
        .collect();
    assert_eq!(releases.len(), 2);
    assert!(
        releases
            .iter()
            .all(|r| r.value == KEY_RELEASE && r.flags == KEY_SYNTHETIC && r.device == k)
    );
    assert_eq!(status(&sent, k), vec![DeviceStatus::Gone]);
    // Shift stays down behind A, in order, until A is decided.
    assert!(t.w.virtual_down().contains(KEY_LEFTSHIFT));
    t.verdict(a, false);
    assert!(t.w.virtual_down().is_empty());
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_LEFTSHIFT, 1),
            (KEY_A, 1),
            (KEY_A, 0),
            (KEY_LEFTSHIFT, 0)
        ]
    );
    assert!(t.w.grabbed(k2), "the other keyboard is still held");
}

#[test]
fn a_keyboard_that_vanishes_with_nothing_waiting_has_its_keys_released_at_once() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTCTRL);
    let seq = t.last_seq();
    t.verdict(seq, false);
    t.w.unplug(k);
    t.core.watch_ready(t.now);
    t.take();
    t.core.keyboard_ready(k, t.now);
    t.take();
    assert!(t.w.virtual_down().is_empty());
    // A keyboard plugged in again gets a new number and is taken as new.
    let again = t.w.plug("AT Translated Set 2 keyboard");
    t.core.watch_ready(t.now);
    t.take();
    assert!(t.w.grabbed(again));
}

#[test]
fn keys_lost_with_dropped_events_are_released() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    t.press(k, KEY_A);
    for key in t.keys() {
        t.verdict(key.seq, false);
    }
    t.w.overflow(k, &[KEY_LEFTSHIFT]);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let keys = t.keys();
    assert_eq!(keys.len(), 1);
    assert_eq!(
        (keys[0].code, keys[0].value, keys[0].flags),
        (KEY_LEFTSHIFT, KEY_RELEASE, KEY_SYNTHETIC)
    );
    assert_eq!(t.w.virtual_down(), [KEY_A].into_iter().collect());
    t.release(k, KEY_A);
    assert!(t.w.virtual_down().is_empty());
}

// The session.

#[test]
fn the_session_leaving_the_front_lets_go_and_its_return_grabs_again() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTCTRL);
    let seq = t.last_seq();
    t.verdict(seq, false);
    // Control, Alt and F2 switch to a console, where logind makes another session active.
    t.core.front_changed(front("c1", 1000), t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    t.assert_let_go();
    assert!(!t.w.is_open(k));
    t.pass(Duration::from_secs(10));
    t.send(FromClient::Heartbeat { counter: 0 });
    assert!(!t.w.is_open(k));
    // Back again, with Control still down from the switch.
    t.core.front_changed(front(SESSION, 1000), t.now);
    t.take();
    assert!(t.w.is_open(k) && !t.w.grabbed(k));
    t.w.release(k, KEY_LEFTCTRL);
    t.pass(KEYS_UP_RETRY);
    assert!(t.w.grabbed(k));
}

impl T {
    fn console(&mut self, n: u32) {
        self.core.console_changed(n, self.now);
        self.take();
    }

    fn logind(&mut self, f: Front) {
        self.core.front_changed(f, self.now);
        self.take();
    }
}

#[test]
fn a_switch_of_consoles_lets_go_before_logind_says_so_and_only_logind_grabs_again() {
    let (mut t, k) = T::capturing();
    t.console(X_CONSOLE);
    assert!(t.w.grabbed(k));
    t.press(k, KEY_LEFTSHIFT);
    let seq = t.last_seq();
    t.verdict(seq, false);
    t.keys();
    // The kernel switches to console 3, and the helper lets go at once.
    t.console(3);
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    t.assert_let_go();
    // logind, not yet aware, still names the session it named before: nothing is grabbed.
    t.logind(front(SESSION, 1000));
    t.pass(Duration::from_secs(1));
    assert!(!t.w.is_open(k));
    // logind catches up: nobody is in front on console 3.
    t.logind(Front::Nobody);
    assert!(!t.w.is_open(k));
    // The kernel switches back: not enough on its own.
    t.console(X_CONSOLE);
    t.pass(Duration::from_secs(1));
    assert!(!t.w.is_open(k), "grabbed again on the kernel's word alone");
    t.w.release(k, KEY_LEFTSHIFT);
    t.logind(front(SESSION, 1000));
    assert!(t.w.grabbed(k));
}

#[test]
fn a_switch_away_and_back_before_logind_speaks_grabs_again_when_it_does() {
    let (mut t, k) = T::capturing();
    t.console(X_CONSOLE);
    t.console(3);
    t.assert_let_go();
    t.console(X_CONSOLE);
    assert!(!t.w.is_open(k));
    // logind, asked again, names the client's session, which the kernel agrees is in front.
    t.logind(front(SESSION, 1000));
    assert!(t.w.grabbed(k));
}

#[test]
fn without_consoles_logind_alone_says_what_is_in_front() {
    let (mut t, k) = T::capturing();
    t.logind(front(SESSION, 1000));
    assert!(t.w.grabbed(k));
    t.logind(front("c1", 1000));
    t.assert_let_go();
    t.logind(front(SESSION, 1000));
    assert!(t.w.grabbed(k));
}

#[test]
fn not_knowing_which_session_is_in_front_lets_go() {
    let (mut t, k) = T::capturing();
    t.core.front_changed(Front::Unknown, t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::SessionUnknown)]);
    t.assert_let_go();
    t.core.front_changed(front(SESSION, 1000), t.now);
    t.take();
    assert!(t.w.grabbed(k));
}

// The virtual keyboard failing while in use, other grabbers, and stopping.

#[test]
fn a_virtual_keyboard_that_stops_taking_keys_is_destroyed_and_everything_let_go() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    let seq = t.last_seq();
    t.verdict(seq, false);
    t.w.fail_emit(true);
    t.press(k, KEY_A);
    let seq = t.last_seq();
    t.verdict(seq, false);
    assert!(t.sent().contains(&released(Released::VirtualKeyboard)));
    t.assert_let_go();
    assert!(!t.w.virtual_exists());
    // The kernel released Shift as it destroyed the device.
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTSHIFT, 1), (KEY_LEFTSHIFT, 0)]);
    t.w.fail_emit(false);
    t.w.release(k, KEY_A);
    t.w.release(k, KEY_LEFTSHIFT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert!(t.w.grabbed(k));
    assert_eq!(t.w.virtual_made(), 2);
}

#[test]
fn a_keyboard_another_program_holds_is_tried_again() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.set_busy(k, true);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert_eq!(status(&t.sent(), k), vec![DeviceStatus::Busy]);
    assert!(!t.core.holding());
    t.pass(BUSY_RETRY);
    assert!(t.sent().is_empty(), "said once");
    t.w.set_busy(k, false);
    t.pass(BUSY_RETRY);
    assert_eq!(status(&t.sent(), k), vec![DeviceStatus::Held]);
}

#[test]
fn stop_lets_go_and_releases_and_start_takes_again() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    let seq = t.last_seq();
    t.verdict(seq, false);
    t.send(FromClient::Stop);
    assert_eq!(t.sent(), vec![released(Released::Stopped)]);
    t.assert_let_go();
    assert!(!t.w.is_open(k));
    t.w.release(k, KEY_LEFTSHIFT);
    t.send(FromClient::Start);
    assert!(t.w.grabbed(k));
    // A STOP with nothing held is confirmed all the same.
    t.send(FromClient::Stop);
    t.send(FromClient::Stop);
    assert_eq!(
        t.sent()
            .iter()
            .filter(|m| **m == released(Released::Stopped))
            .count(),
        2
    );
}

// Which devices are taken.

#[test]
fn keyboards_that_come_later_are_taken_and_other_devices_are_not() {
    let (mut t, _) = T::capturing();
    let usb = t.w.plug("USB keyboard");
    let mut touchpad_desc = keyboard("Logitech K400");
    touchpad_desc.types |= 1 << EV_REL;
    let touchpad = t.w.plug_device(touchpad_desc);
    let power = t.w.plug_device(description(
        "Power Button",
        &[EV_SYN, EV_KEY],
        [KEY_POWER].into_iter().collect(),
    ));
    let mut remapper_desc = keyboard("keyd virtual keyboard");
    remapper_desc.parentless = true;
    let remapper = t.w.plug_device(remapper_desc);
    t.core.watch_ready(t.now);
    t.take();
    let sent = t.sent();
    assert_eq!(status(&sent, usb), vec![DeviceStatus::Held]);
    assert_eq!(status(&sent, touchpad), vec![DeviceStatus::Ignored]);
    assert_eq!(status(&sent, remapper), vec![DeviceStatus::Ignored]);
    assert!(status(&sent, power).is_empty());
    assert!(!t.w.is_open(touchpad) && !t.w.is_open(power) && !t.w.is_open(remapper));
    assert!(sent.contains(&ToClient::Device {
        device: touchpad,
        status: DeviceStatus::Ignored,
        detail: Ignored::Pointer as u8,
        name: "Logitech K400".into()
    }));
    // A scan with nothing new says nothing again.
    t.pass(RESCAN_INTERVAL);
    assert!(t.sent().is_empty());
}

// What the watchdog and the loop are told.

#[test]
fn the_oldest_undecided_key_and_the_next_deadline_are_known() {
    let (mut t, k) = T::capturing();
    assert_eq!(t.core.oldest_undecided(), None);
    let read = t.now;
    t.press(k, KEY_A);
    t.pass(ms(10));
    t.press(k, KEY_T);
    assert_eq!(t.core.oldest_undecided(), Some(read));
    assert_eq!(t.core.next_wake(), Some(read + VERDICT_TIMEOUT));
    for key in t.keys() {
        t.verdict(key.seq, true);
    }
    assert_eq!(t.core.oldest_undecided(), None);
    assert!(t.core.holding());
    assert!(t.core.next_wake().is_some(), "the heartbeat is always due");
    let set = t.core.poll_set();
    assert!(set.iter().any(|(s, _)| *s == Source::Keyboard(k)));
    assert!(set.iter().any(|(s, _)| *s == Source::Virtual));
    assert!(set.iter().any(|(s, _)| *s == Source::Watch));
}

// Found in the security review of admission and the session in front. Each fails on purpose and
// says in its message what goes wrong.

#[test]
fn review_logind_before_the_kernel_on_a_switch_back_does_not_shut_the_reader_out() {
    let (mut t, k) = T::capturing();
    t.console(7);
    // Away to console 2, where nobody is in front; the kernel and logind agree.
    t.console(2);
    t.logind(Front::Nobody);
    t.assert_let_go();
    // Back to console 7. Both learn of it from the same sysfs notification; here logind's answer
    // reaches the loop before the console thread's.
    t.logind(front(SESSION, 1000));
    t.console(7);
    // From now on logind says the same thing every SESSION_RECHECK, and the kernel nothing more.
    for _ in 0..5 {
        t.pass(SESSION_RECHECK);
        t.logind(front(SESSION, 1000));
    }
    assert!(
        t.w.grabbed(k),
        "the client's session is in front on the console logind and the kernel agree on, yet the \
         helper takes every later answer from logind for a stale one and never captures again"
    );
}

#[test]
fn review_a_client_whose_session_is_not_in_front_hears_nothing_of_the_one_that_is() {
    let (mut t, _k) = T::capturing();
    // Another user's session comes to the front; the old client stays connected, as it may.
    t.logind(front("5", 1001));
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    // The other user presses Caps Lock, and their X server lights it on the virtual keyboard.
    t.desktop_leds(LOCK_CAPS);
    let sent = t.sent();
    assert!(
        sent.is_empty(),
        "the client of a session in the background was told of the lock lights of the session in \
         front: {sent:?}"
    );
}

#[test]
fn review_callers_that_hang_up_leave_no_admission_work_behind() {
    let mut t = T::new();
    // logind cannot be asked for a moment: at the helper's start, or while logind or the bus
    // restarts. Every user's caller is let in while it lasts.
    t.logind(Front::Unknown);
    let stranger = Peer {
        pid: 5000,
        uid: 1001,
        gid: 1001,
    };
    for c in 0..1000u64 {
        t.connect(100 + c, stranger);
        t.hello(100 + c);
        t.core.disconnected(100 + c, t.now);
        t.take();
    }
    let asked = t
        .out
        .iter()
        .filter(|o| matches!(o, Output::Admit(..)))
        .count();
    assert!(
        asked <= MAX_CALLERS,
        "another user's process queued {asked} questions to logind and polkit, each still asked \
         after its connection had gone, ahead of the rightful client's, which waits behind them \
         past ADMISSION_TIMEOUT"
    );
}

// Found in review: these fail while the fault they name stands.

/// The resync after a loss of events puts in `down` no key that went down unseen, and the chord is
/// looked for in `down` alone, so a chord whose first keys went down while the loop was behind is
/// never seen however long all three are held: only letting go of them and pressing them again
/// sets it off. A loop falls behind like this in a stall shorter than the watchdog's limit, which is
/// when the chord is most wanted.
#[test]
fn fault_the_chord_held_through_lost_events_lets_go() {
    let (mut t, k) = T::capturing();
    // Backspace and Escape go down while the helper is behind, and the kernel's buffer overflows.
    t.w.press(k, KEY_BACKSPACE);
    t.w.press(k, KEY_ESC);
    t.w.overflow(k, &[]);
    t.core.keyboard_ready(k, t.now);
    t.take();
    // Enter joins them, and all three are held.
    t.press(k, KEY_ENTER);
    t.pass(VERDICT_TIMEOUT * 5);
    let sent = t.sent();
    assert!(
        sent.contains(&released(Released::Emergency)),
        "Backspace, Escape and Enter are all held on a held keyboard, and the helper still holds it: {sent:?}"
    );
    t.assert_let_go();
}
