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
fn a_forgiven_freeze_gives_the_virtual_keyboard_its_time_afresh() {
    let s = Duration::from_secs;
    // Frozen while the virtual keyboard settles: not grabbed until it has settled from the thaw.
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE / 2);
    t.now += s(10);
    assert!(t.core.thawed(t.now));
    t.pass(VIRTUAL_SETTLE - ms(1));
    assert!(
        !t.w.grabbed(k),
        "grabbed before the desktop could take it up"
    );
    t.pass(ms(1));
    assert!(t.w.grabbed(k));
    // Frozen while it has yet to appear: not given up for the frozen time.
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.never_appears(true);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_APPEAR_TIMEOUT / 2);
    t.now += s(10);
    assert!(t.core.thawed(t.now));
    t.w.never_appears(false);
    t.pass(VIRTUAL_SETTLE + HEARTBEAT_INTERVAL);
    assert!(!t.sent().contains(&released(Released::VirtualKeyboard)));
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

#[test]
fn a_keyboard_that_will_not_be_let_go_of_is_closed_and_found_again() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.press_at_grab(k, KEY_A);
    t.w.fail_ungrab(k, true);
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert_eq!(t.w.grabs(k), 1);
    assert!(
        !t.w.grabbed(k),
        "left grabbed by a file nobody reads or revokes"
    );
    t.w.fail_ungrab(k, false);
    t.pass(KEYS_UP_RETRY);
    assert!(t.w.is_open(k), "not found again");
    t.release(k, KEY_A);
    t.pass(KEYS_UP_RETRY);
    assert!(t.w.grabbed(k));
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

// WAIT, with FEATURE_WAIT.

impl T {
    /// As capturing, with FEATURE_WAIT asked for and granted.
    fn waiting() -> (T, u32) {
        let mut t = T::new();
        let k = t.w.plug("AT Translated Set 2 keyboard");
        t.connect(CLIENT, PEER);
        t.from(
            CLIENT,
            FromClient::Hello {
                version: PROTOCOL_VERSION,
                features: FEATURE_WAIT,
            },
        );
        t.core.admitted(CLIENT, Ok(SESSION.into()), t.now);
        t.take();
        let welcome = t.sent();
        assert!(
            matches!(
                welcome[0],
                ToClient::Welcome {
                    features: FEATURE_WAIT,
                    ..
                }
            ),
            "{welcome:?}"
        );
        t.send(FromClient::Start);
        t.pass(VIRTUAL_SETTLE);
        assert!(t.w.grabbed(k));
        t.out.clear();
        (t, k)
    }

    fn wait(&mut self, seq: u64) {
        self.send(FromClient::Wait { seq });
    }

    fn expired(&mut self) -> Vec<u64> {
        self.sent()
            .into_iter()
            .filter_map(|m| match m {
                ToClient::Expired { seq } => Some(seq),
                _ => None,
            })
            .collect()
    }
}

#[test]
fn a_press_held_by_wait_is_kept_or_passed_as_answered_within_the_longer_bound() {
    let (mut t, k) = T::waiting();
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.wait(a);
    t.pass(VERDICT_WAIT_TIMEOUT - ms(10));
    assert!(t.w.emitted().is_empty());
    assert!(t.expired().is_empty());
    t.verdict(a, true);
    t.release(k, KEY_A);
    t.pass(ms(20));
    assert!(t.w.emitted().is_empty(), "the kept key went out");
    t.press(k, KEY_T);
    let b = t.last_seq();
    t.wait(b);
    t.pass(VERDICT_WAIT_TIMEOUT - ms(10));
    assert!(t.w.emitted().is_empty());
    t.verdict(b, false);
    assert_eq!(t.w.emitted(), vec![(KEY_T, 1)]);
    t.release(k, KEY_T);
    assert_eq!(t.w.emitted(), vec![(KEY_T, 1), (KEY_T, 0)]);
    assert!(t.expired().is_empty());
}

#[test]
fn a_press_held_by_wait_is_passed_at_the_longer_bound_with_later_keys_in_order_behind_it() {
    let (mut t, k) = T::waiting();
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.wait(a);
    // Shift comes behind it and is answered; T comes behind that and is not.
    t.pass(ms(50));
    t.press(k, KEY_LEFTSHIFT);
    let shift = t.last_seq();
    t.verdict(shift, false);
    t.press(k, KEY_T);
    let late = t.last_seq();
    t.release(k, KEY_A);
    t.release(k, KEY_T);
    t.release(k, KEY_LEFTSHIFT);
    t.pass(VERDICT_WAIT_TIMEOUT - ms(60));
    assert!(t.w.emitted().is_empty(), "went out before the longer bound");
    t.pass(ms(10));
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_A, 1),
            (KEY_LEFTSHIFT, 1),
            (KEY_T, 1),
            (KEY_A, 0),
            (KEY_T, 0),
            (KEY_LEFTSHIFT, 0)
        ]
    );
    assert_eq!(t.expired(), vec![a, late]);
    assert!(t.w.virtual_down().is_empty());
}

#[test]
fn only_the_first_wait_counts_and_only_while_the_press_waits() {
    let (mut t, k) = T::waiting();
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.wait(a);
    t.pass(ms(500));
    // A second WAIT does not move the bound again.
    t.wait(a);
    t.pass(VERDICT_WAIT_TIMEOUT - ms(500));
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    assert_eq!(t.expired(), vec![a]);
    // A WAIT for a press already passed on at its bound changes nothing.
    t.release(k, KEY_A);
    t.press(k, KEY_T);
    let b = t.last_seq();
    t.pass(VERDICT_TIMEOUT);
    t.wait(b);
    assert_eq!(t.expired(), vec![b]);
    // Nor does one for a press already answered.
    t.release(k, KEY_T);
    t.press(k, KEY_LEFTSHIFT);
    let c = t.last_seq();
    t.verdict(c, true);
    t.wait(c);
    t.pass(VERDICT_WAIT_TIMEOUT);
    assert!(t.expired().is_empty());
    assert!(t.core.client.is_some());
}

#[test]
fn a_wait_without_the_feature_breaks_the_protocol() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.wait(a);
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Malformed));
    t.assert_let_go();
}

#[test]
fn a_client_that_only_ever_says_wait_is_dropped_as_one_that_says_nothing() {
    // Someone typing ten keys a second at a client that answers every press WAIT and nothing more.
    let typed = |wait: bool| {
        let (mut t, k) = T::waiting();
        let start = t.now;
        for _ in 0..40 {
            t.press(k, KEY_A);
            if wait {
                let seq = t.last_seq();
                t.wait(seq);
            }
            t.pass(ms(50));
            t.release(k, KEY_A);
            t.pass(ms(50));
            if goodbye_reason(&t.sent()) == Some(Goodbye::Unanswered) {
                t.assert_let_go();
                return t.now - start;
            }
        }
        panic!("not dropped after four seconds of typing");
    };
    let silent = typed(false);
    let waiting = typed(true);
    assert!(silent <= UNANSWERED_SPAN + ms(100), "{silent:?}");
    assert!(
        waiting <= UNANSWERED_SPAN + VERDICT_WAIT_TIMEOUT,
        "{waiting:?}, a client saying nothing {silent:?}"
    );
}

#[test]
fn the_emergency_chord_goes_past_a_press_held_by_wait() {
    let (mut t, k) = T::waiting();
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.wait(a);
    for c in CHORD.map(|keys| keys[0]) {
        t.press(k, c);
    }
    assert!(t.sent().contains(&released(Released::Emergency)));
    t.assert_let_go();
    assert!(t.w.emitted().is_empty(), "the held press went out");
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
    for c in CHORD.map(|keys| keys[0]) {
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
    for c in CHORD.map(|keys| keys[0]) {
        t.w.press(k, c);
    }
    t.w.overflow(k, &[]);
    t.core.keyboard_ready(k, t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::Emergency)]);
    t.assert_let_go();
}

const KEY_G: u16 = 34;
const KEY_H: u16 = 35;

/// A key typed after the loop last read a keyboard and before the helper lets go of it is read once
/// more as the keyboard is let go of and passed on, whatever the reason for letting go; the mock
/// keeps such a key in the keyboard's queue, unread, as the kernel keeps it for the grabbing file.
#[test]
fn a_key_typed_as_the_helper_lets_go_is_passed_on_and_nothing_left_down() {
    type LetGo = Box<dyn Fn(&mut T)>;
    let ways: Vec<(&str, LetGo)> = vec![
        ("STOP", Box::new(|t| t.send(FromClient::Stop))),
        (
            "the client gone",
            Box::new(|t| {
                t.core.disconnected(CLIENT, t.now);
                t.take();
            }),
        ),
        (
            "the client silent",
            Box::new(|t| t.quiet(HEARTBEAT_TIMEOUT + HEARTBEAT_INTERVAL)),
        ),
        ("another session", Box::new(|t| t.logind(front("5", 1001)))),
        (
            "sleep",
            Box::new(|t| {
                t.core.sleeping(true, t.now);
                t.take();
            }),
        ),
    ];
    for (why, let_go) in ways {
        let (mut t, k) = T::capturing();
        // G is tapped, and H pressed and still held, the loop having read neither.
        t.w.press(k, KEY_G);
        t.w.release(k, KEY_G);
        t.w.press(k, KEY_H);
        let_go(&mut t);
        assert!(!t.w.any_grabbed(), "{why}");
        assert_eq!(
            t.w.emitted(),
            vec![(KEY_G, 1), (KEY_G, 0), (KEY_H, 1), (KEY_H, 0)],
            "{why}"
        );
        assert!(t.w.virtual_down().is_empty(), "{why}");
    }
}

#[test]
fn letting_go_puts_out_what_was_typed_in_the_order_typed() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    let k2 = t.w.plug("second keyboard");
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    assert!(t.w.grabbed(k) && t.w.grabbed(k2));
    t.pressed_out(k, KEY_LEFTSHIFT);
    // The chord on one keyboard, G after it in the same batch; H on the other, not yet read.
    for c in CHORD.map(|keys| keys[0]) {
        t.w.press(k, c);
    }
    t.w.press(k, KEY_G);
    t.w.press(k2, KEY_H);
    t.core.keyboard_ready(k, t.now);
    t.take();
    assert!(t.sent().contains(&released(Released::Emergency)));
    t.assert_let_go();
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_LEFTSHIFT, 1),
            (KEY_LEFTSHIFT, 0),
            (KEY_G, 1),
            (KEY_G, 0),
            (KEY_H, 1),
            (KEY_H, 0)
        ]
    );
}

/// What letting go taps the client is told, as a press and a release without flags, in the order
/// they went out and before RELEASED: after the chord the rest of its batch, then each keyboard's
/// last read, each with its keyboard's number.
#[test]
fn the_keys_tapped_as_the_helper_lets_go_are_told_before_released() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    let k2 = t.w.plug("second keyboard");
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    t.out.clear();
    for c in CHORD.map(|keys| keys[0]) {
        t.w.press(k, c);
    }
    t.w.press(k, KEY_G);
    t.w.press(k2, KEY_H);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let after_chord: Vec<ToClient> = t
        .sent()
        .into_iter()
        .skip_while(|m| !matches!(m, ToClient::Key(k) if k.code == KEY_ESC))
        .skip(1)
        .collect();
    let tap = |m: &ToClient| match m {
        ToClient::Key(key) => Some((key.device, key.code, key.value, key.flags)),
        _ => None,
    };
    assert_eq!(
        after_chord.iter().map(tap).collect::<Vec<_>>(),
        vec![
            Some((k, KEY_G, KEY_PRESS, 0)),
            Some((k, KEY_G, KEY_RELEASE, 0)),
            Some((k2, KEY_H, KEY_PRESS, 0)),
            Some((k2, KEY_H, KEY_RELEASE, 0)),
            None,
        ]
    );
    assert_eq!(after_chord[4], released(Released::Emergency));

    // On STOP, the same.
    let (mut t, k) = T::capturing();
    t.w.press(k, KEY_G);
    t.send(FromClient::Stop);
    let told: Vec<Option<(u32, u16, u8, u8)>> = t.sent().iter().map(tap).collect();
    assert_eq!(
        told,
        vec![
            Some((k, KEY_G, KEY_PRESS, 0)),
            Some((k, KEY_G, KEY_RELEASE, 0)),
            None
        ]
    );
    assert_eq!(t.w.emitted(), vec![(KEY_G, 1), (KEY_G, 0)]);

    // Let go of because another session came in front, the client is told nothing of a key that
    // went to that session.
    let (mut t, k) = T::capturing();
    t.w.press(k, KEY_G);
    t.core.front_changed(front("c1", 1000), t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    assert_eq!(t.w.emitted(), vec![(KEY_G, 1), (KEY_G, 0)]);

    // Nor is a client dropped told anything.
    let (mut t, k) = T::capturing();
    t.w.press(k, KEY_G);
    t.core.disconnected(CLIENT, t.now);
    t.take();
    assert!(t.everything_sent_to(CLIENT).is_empty());
    assert_eq!(t.w.emitted(), vec![(KEY_G, 1), (KEY_G, 0)]);
}

#[test]
fn keys_read_after_the_emergency_chord_in_the_same_batch_are_passed_on() {
    let (mut t, k) = T::capturing();
    for c in CHORD.map(|keys| keys[0]) {
        t.w.press(k, c);
    }
    t.w.press(k, KEY_G);
    t.w.release(k, KEY_G);
    t.core.keyboard_ready(k, t.now);
    t.take();
    assert!(t.sent().contains(&released(Released::Emergency)));
    t.assert_let_go();
    assert_eq!(t.w.emitted(), vec![(KEY_G, 1), (KEY_G, 0)]);
}

/// The emergency chord after a loss of events, in each order of loss, press and the helper's
/// question to the kernel: keys pressed in the lost stretch, keys whose press was read with the
/// report that ended the loss, keys pressed between that read and the question, and keys pressed
/// after the question.
#[test]
fn the_chord_lets_go_whenever_its_keys_went_down_around_a_loss_of_events() {
    let chord = CHORD.map(|keys| keys[0]);
    // Each key in turn is pressed in the lost stretch (0), read with the report (1), pressed
    // before the question (2) or after it (3).
    for order in 0..4usize.pow(3) {
        let (mut t, k) = T::capturing();
        let when: Vec<usize> = (0..3).map(|i| order / 4usize.pow(i as u32) % 4).collect();
        for (c, w) in chord.iter().zip(&when) {
            if *w == 0 {
                t.w.press(k, *c);
            }
        }
        t.w.overflow(k, &[]);
        for (c, w) in chord.iter().zip(&when) {
            match w {
                1 => t.w.press(k, *c),
                2 => t.w.change_unread(k, *c, true),
                _ => {}
            }
        }
        t.core.keyboard_ready(k, t.now);
        t.take();
        for (c, w) in chord.iter().zip(&when) {
            if *w == 3 {
                t.press(k, *c);
            }
        }
        assert!(
            t.sent().contains(&released(Released::Emergency)),
            "chord keys at {when:?} (0 lost, 1 read, 2 before the question, 3 after)"
        );
        t.assert_let_go();
    }
}

#[test]
fn a_key_read_after_a_loss_and_let_go_of_before_the_question_is_not_left_down() {
    let (mut t, k) = T::capturing();
    t.w.overflow(k, &[]);
    t.w.press(k, KEY_A);
    t.w.change_unread(k, KEY_A, false);
    t.core.keyboard_ready(k, t.now);
    t.take();
    // Read after the loss, its press is left alone, and the kernel has it up: it is lost whole.
    assert!(t.keys().is_empty());
    t.pass(VERDICT_TIMEOUT);
    assert!(t.w.emitted().is_empty());
    assert!(t.w.virtual_down().is_empty());
}

#[test]
fn the_numpads_enter_serves_the_emergency_chord_as_well() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_BACKSPACE);
    t.press(k, KEY_ESC);
    t.press(k, KEY_KPENTER);
    assert!(t.sent().contains(&released(Released::Emergency)));
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
    for c in CHORD.map(|keys| keys[0]) {
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

// Sleep.

#[test]
fn going_to_sleep_lets_go_and_waking_captures_again_once_every_key_is_up() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    let seq = t.last_seq();
    t.verdict(seq, false);
    t.keys();
    t.core.sleeping(true, t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    t.assert_let_go();
    assert!(t.core.client.is_some() && !t.closed(CLIENT));
    // logind's word on the session in front captures nothing while asleep.
    t.logind(front(SESSION, 1000));
    t.pass(Duration::from_secs(3));
    assert!(!t.w.is_open(k));
    // Woken with Shift still down: grabbed only once it is up.
    t.core.sleeping(false, t.now);
    t.take();
    assert!(t.w.is_open(k) && !t.w.grabbed(k));
    t.w.release(k, KEY_LEFTSHIFT);
    t.pass(KEYS_UP_RETRY);
    assert!(t.w.grabbed(k));
}

#[test]
fn waking_starts_the_clients_silence_afresh() {
    let (mut t, k) = T::capturing();
    t.core.sleeping(true, t.now);
    t.take();
    // Every process frozen for five seconds: no turn of the loop, no heartbeat.
    t.now += Duration::from_secs(5);
    t.core.sleeping(false, t.now);
    t.core.tick(t.now);
    t.take();
    assert!(!t.closed(CLIENT), "{:?}", t.out);
    assert!(t.w.grabbed(k));
    t.quiet(HEARTBEAT_TIMEOUT);
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Silent));
}

#[test]
fn a_forgiven_freeze_gives_the_client_and_a_waiting_press_their_bounds_afresh() {
    let (mut t, k) = T::capturing();
    // The client last heard 1.9 s before, and a press read a millisecond before every process
    // stops; the loop, waiting for the press's bound, wakes three seconds late, as Server::turn has
    // it, and the first tick after the thaw comes at once.
    t.send(FromClient::Heartbeat { counter: 0 });
    t.now += HEARTBEAT_TIMEOUT - ms(100);
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.now += Duration::from_secs(3);
    assert!(t.core.thawed(t.now));
    t.core.tick(t.now);
    t.take();
    assert!(!t.closed(CLIENT), "dropped for the frozen time");
    assert!(t.w.emitted().is_empty(), "passed on for the frozen time");
    // The press has its whole bound from the thaw.
    t.now += VERDICT_TIMEOUT - ms(1);
    t.core.tick(t.now);
    t.take();
    assert!(t.w.emitted().is_empty());
    t.verdict(a, false);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    // And the client its whole silence: a hung one keeps the keyboard no longer than the frozen
    // time and twice the heartbeat's bound from its last word.
    let thaw = t.now - (VERDICT_TIMEOUT - ms(1));
    t.quiet(HEARTBEAT_TIMEOUT - VERDICT_TIMEOUT);
    assert!(!t.closed(CLIENT));
    t.quiet(VERDICT_TIMEOUT);
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Silent));
    t.assert_let_go();
    // Its last word came 1.9 s and a freeze before the thaw, and it was dropped one bound after it.
    assert!(
        t.now - thaw <= HEARTBEAT_TIMEOUT + VERDICT_TIMEOUT,
        "{:?}",
        t.now - thaw
    );
}

#[test]
fn a_late_wake_that_is_not_forgiven_changes_nothing() {
    let (mut t, k) = T::capturing();
    assert!(t.core.thawed(t.now));
    t.press(k, KEY_A);
    t.now += Duration::from_secs(3);
    assert!(!t.core.thawed(t.now), "forgiven twice within the minute");
    t.core.tick(t.now);
    t.take();
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Silent));
    t.assert_let_go();
}

#[test]
fn a_freeze_logind_did_not_announce_is_forgiven_once_in_a_while_and_an_announced_one_always() {
    let (mut t, _k) = T::capturing();
    let s = Duration::from_secs;
    assert!(t.core.thawed(t.now));
    t.now += s(10);
    assert!(!t.core.thawed(t.now), "forgiven again within the minute");
    t.now += FREEZE_FORGIVEN_EVERY;
    assert!(t.core.thawed(t.now));
    t.core.sleeping(true, t.now);
    for _ in 0..5 {
        t.now += s(1);
        assert!(t.core.thawed(t.now), "an announced sleep not forgiven");
    }
    t.core.sleeping(false, t.now);
    t.now += SLEEP_GRACE;
    assert!(t.core.thawed(t.now));
    t.now += s(1);
    assert!(!t.core.thawed(t.now));
}

#[test]
fn on_a_starved_machine_a_press_is_passed_on_within_two_bounds_and_two_late_wakes() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    let read = t.now;
    let late = Duration::from_secs(1);
    // Every wake of the loop a second late, each taken for a freeze as Server::turn does.
    while t.w.emitted().is_empty() {
        let next = t.core.next_wake().unwrap().min(t.now + LOOP_TICK);
        let woke = next.max(t.now) + late;
        t.core.thawed(woke);
        t.now = woke;
        t.send(FromClient::Heartbeat { counter: 0 });
        t.core.tick(t.now);
        t.take();
    }
    assert!(
        t.now - read <= 2 * VERDICT_TIMEOUT + 2 * late,
        "{:?}",
        t.now - read
    );
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

// Found in the second review. Each is ignored, so that the package's checks still pass, and fails
// with cargo test -p linda-keyd -- --ignored while the fault it names stands.

const KEY_TAB: u16 = 15;
/// The desktop repeats a key held down longer than this: org.mate.peripherals-keyboard's delay in
/// mate-settings-daemon 1.28, and GNOME's; a bare X server waits 660 ms.
const DESKTOP_REPEAT_DELAY: Duration = Duration::from_millis(500);

/// A press answered WAIT holds every key read after it for up to VERDICT_WAIT_TIMEOUT, releases
/// included. A key that went out on the virtual keyboard before it and was let go of after it,
/// as in any rolled keystroke, stays down there all that time, past the desktop's repeat delay, so
/// the desktop repeats it: Tab pressed once moves the focus several times, Enter sends twice. Without
/// WAIT a release waits at most VERDICT_TIMEOUT, well under any repeat delay.
#[test]
fn fault_a_key_let_go_of_is_not_held_down_past_the_repeat_delay_behind_a_wait() {
    let (mut t, k) = T::waiting();
    t.press(k, KEY_TAB);
    let tab = t.last_seq();
    t.verdict(tab, false);
    assert!(t.w.virtual_down().contains(KEY_TAB));
    let tab_out = t.now;
    // The next key goes down before Tab comes up, and the client, following the focus Tab moved,
    // answers it WAIT.
    t.pass(ms(30));
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.wait(a);
    t.pass(ms(20));
    t.release(k, KEY_TAB);
    let let_go = t.now;
    t.pass(DESKTOP_REPEAT_DELAY - (let_go - tab_out));
    assert!(
        !t.w.virtual_down().contains(KEY_TAB),
        "Tab, pressed once and let go of {} ms ago, has been held down on the virtual keyboard for \
         {} ms behind a press held by WAIT, and the desktop repeats a key held down for {} ms",
        (t.now - let_go).as_millis(),
        (t.now - tab_out).as_millis(),
        DESKTOP_REPEAT_DELAY.as_millis()
    );
}

/// Before WAIT no press could still be in line once its own bound had passed, so a verdict after
/// the bound was a verdict on a key already passed on. Behind a press held by WAIT a press stays in
/// line long after its own VERDICT_TIMEOUT, and a verdict that comes for it then, while nothing has
/// yet been done with it, is thrown away: the press is passed on when the held one is decided. A
/// reader that answers keep for a command key it could not decide sooner, because it decides in
/// order and the key before is waiting on the focus, runs the command and the application gets the
/// key as well.
#[test]
fn fault_a_keep_for_a_press_still_in_line_behind_a_wait_is_not_thrown_away() {
    let (mut t, k) = T::waiting();
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.wait(a);
    t.pass(ms(10));
    t.press(k, KEY_INSERT);
    let insert = t.last_seq();
    t.pass(VERDICT_TIMEOUT + ms(50));
    assert!(t.w.emitted().is_empty(), "nothing has gone out yet");
    // The command key is answered while A still holds the line, and then A.
    t.verdict(insert, true);
    assert!(t.w.emitted().is_empty(), "nothing has gone out yet");
    t.verdict(a, false);
    t.pass(VERDICT_WAIT_TIMEOUT);
    assert!(
        !t.w.emitted().contains(&(KEY_INSERT, 1)),
        "the client answered keep for Insert while it still waited in line, before anything was done \
         with it, and Insert went to the application all the same: {:?}",
        t.w.emitted()
    );
}

/// The second review's test of a key pressed after a loss of events, as the fifth and sixth waves'
/// rule has it. G, its press read with the loss and so left alone, is down by the kernel's answer
/// and was not told down: it is unseen, and not told, since when it went down, and so under which
/// modifiers, cannot be known. Once let go of and pressed again, it is an ordinary key. A modifier
/// is told instead, as below.
#[test]
fn a_key_held_across_a_loss_of_events_is_seen_once_pressed_again() {
    let (mut t, k) = T::capturing();
    t.w.overflow(k, &[]);
    t.w.press(k, KEY_G);
    t.core.keyboard_ready(k, t.now);
    t.take();
    t.press(k, KEY_A);
    t.release(k, KEY_A);
    t.release(k, KEY_G);
    let keys: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    assert_eq!(keys, vec![(KEY_A, KEY_PRESS), (KEY_A, KEY_RELEASE)]);
    t.press(k, KEY_G);
    let keys: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    assert_eq!(keys, vec![(KEY_G, KEY_PRESS)]);
}

/// After a loss, the releases of keys told down that the kernel has up come first, then a press,
/// asking and made up, of each key found down that modifies others and was not told down; a key
/// told down and still down is told nothing more, nor is the press of a modifier the answer
/// already counted, read after it. The made-up press is decided like any other.
#[test]
fn a_modifier_found_down_after_a_loss_is_told_after_the_releases_and_decided_like_any_press() {
    let (mut t, k) = T::capturing();
    t.pressed_out(k, KEY_A);
    t.pressed_out(k, KEY_LEFTSHIFT);
    t.w.change_unread(k, KEY_A, false);
    t.w.change_unread(k, KEY_INSERT, true);
    t.w.overflow(k, &[]);
    t.w.press(k, KEY_LEFTCTRL);
    t.w.press(k, KEY_G);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let told = t.keys();
    let keys: Vec<(u16, u8, u8)> = told.iter().map(|k| (k.code, k.value, k.flags)).collect();
    assert_eq!(
        keys,
        vec![
            (KEY_A, KEY_RELEASE, KEY_SYNTHETIC),
            (KEY_LEFTCTRL, KEY_PRESS, KEY_ASK | KEY_SYNTHETIC),
            (KEY_INSERT, KEY_PRESS, KEY_ASK | KEY_SYNTHETIC),
        ]
    );
    let (control, insert) = (told[1].seq, told[2].seq);
    // Control's own press, counted by the answer and read after it.
    t.press(k, KEY_LEFTCTRL);
    assert!(t.keys().is_empty());
    t.verdict(control, false);
    t.verdict(insert, true);
    t.press(k, KEY_C);
    let c = t.keys();
    assert_eq!(
        c.iter()
            .map(|k| (k.code, k.value, k.flags))
            .collect::<Vec<_>>(),
        vec![(KEY_C, KEY_PRESS, KEY_ASK)]
    );
    t.verdict(c[0].seq, false);
    t.release(k, KEY_LEFTCTRL);
    t.release(k, KEY_INSERT);
    let keys: Vec<(u16, u8, u8)> = t
        .keys()
        .iter()
        .map(|k| (k.code, k.value, k.flags))
        .collect();
    assert_eq!(
        keys,
        vec![(KEY_LEFTCTRL, KEY_RELEASE, 0), (KEY_INSERT, KEY_RELEASE, 0)]
    );
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_A, 1),
            (KEY_LEFTSHIFT, 1),
            (KEY_A, 0),
            (KEY_LEFTCTRL, 1),
            (KEY_C, 1),
            (KEY_LEFTCTRL, 0),
        ]
    );
}

/// A made-up press nobody answers is passed at its bound with EXPIRED, as any press.
#[test]
fn a_made_up_press_left_unanswered_is_expired_at_its_bound() {
    let (mut t, k) = T::capturing();
    t.w.overflow(k, &[]);
    t.w.press(k, KEY_LEFTCTRL);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let control = t.last_seq();
    t.pass(VERDICT_TIMEOUT - ms(1));
    assert!(t.expired().is_empty());
    t.pass(ms(1));
    assert_eq!(t.expired(), vec![control]);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTCTRL, 1)]);
}

#[test]
fn a_press_behind_a_held_one_is_expired_only_as_it_is_taken_out_of_line() {
    let (mut t, k) = T::waiting();
    let a = t.held(k, KEY_A);
    t.press(k, KEY_INSERT);
    let insert = t.last_seq();
    t.pass(VERDICT_TIMEOUT * 3);
    assert!(
        t.expired().is_empty(),
        "EXPIRED before Insert left the line"
    );
    t.verdict(a, false);
    assert_eq!(t.expired(), vec![insert]);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_INSERT, 1)]);
    // Out of line, it can be answered no more.
    t.verdict(insert, true);
    t.release(k, KEY_INSERT);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_A, 1), (KEY_INSERT, 1), (KEY_INSERT, 0)]
    );
}

// Releases that go out ahead of presses still waiting.

impl T {
    /// A press of `code` passed on at once, and on the virtual keyboard.
    fn pressed_out(&mut self, k: u32, code: u16) {
        self.press(k, code);
        let seq = self.last_seq();
        self.verdict(seq, false);
        assert!(self.w.virtual_down().contains(code));
    }

    /// A press of `code` answered WAIT; its sequence number.
    fn held(&mut self, k: u32, code: u16) -> u64 {
        self.press(k, code);
        let seq = self.last_seq();
        self.wait(seq);
        seq
    }
}

#[test]
fn a_release_behind_a_waiting_press_is_still_told_to_the_client_in_order() {
    let (mut t, k) = T::waiting();
    t.pressed_out(k, KEY_TAB);
    t.keys();
    t.press(k, KEY_A);
    t.release(k, KEY_TAB);
    t.release(k, KEY_A);
    let told: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    assert_eq!(
        told,
        vec![
            (KEY_A, KEY_PRESS),
            (KEY_TAB, KEY_RELEASE),
            (KEY_A, KEY_RELEASE)
        ]
    );
    assert!(t.w.virtual_down().is_empty(), "Tab waited behind A");
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1), (KEY_TAB, 0)]);
}

#[test]
fn a_key_pressed_again_while_the_line_waits_has_its_second_release_wait_behind_its_second_press() {
    let (mut t, k) = T::waiting();
    t.pressed_out(k, KEY_TAB);
    let a = t.held(k, KEY_A);
    t.release(k, KEY_TAB);
    t.press(k, KEY_TAB);
    let again = t.last_seq();
    t.release(k, KEY_TAB);
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1), (KEY_TAB, 0)]);
    t.verdict(again, false);
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1), (KEY_TAB, 0)]);
    t.verdict(a, false);
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_TAB, 1),
            (KEY_TAB, 0),
            (KEY_A, 1),
            (KEY_TAB, 1),
            (KEY_TAB, 0)
        ]
    );
}

#[test]
fn a_release_whose_press_is_still_in_line_waits_with_it() {
    let (mut t, k) = T::waiting();
    let a = t.held(k, KEY_A);
    t.press(k, KEY_T);
    let tee = t.last_seq();
    t.verdict(tee, false);
    t.release(k, KEY_T);
    assert!(t.w.emitted().is_empty());
    t.verdict(a, true);
    assert_eq!(t.w.emitted(), vec![(KEY_T, 1), (KEY_T, 0)]);
}

#[test]
fn a_modifiers_release_keeps_its_place_and_a_kept_keys_never_goes_out() {
    let (mut t, k) = T::waiting();
    t.pressed_out(k, KEY_LEFTSHIFT);
    t.press(k, KEY_INSERT);
    let insert = t.last_seq();
    t.verdict(insert, true);
    let a = t.held(k, KEY_A);
    t.release(k, KEY_LEFTSHIFT);
    t.release(k, KEY_INSERT);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_LEFTSHIFT, 1)],
        "Shift came up ahead of A"
    );
    t.verdict(a, false);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_LEFTSHIFT, 1), (KEY_A, 1), (KEY_LEFTSHIFT, 0)]
    );
}

#[test]
fn letting_go_with_releases_and_presses_queued_leaves_nothing_down() {
    let (mut t, k) = T::waiting();
    t.pressed_out(k, KEY_TAB);
    t.pressed_out(k, KEY_LEFTSHIFT);
    t.held(k, KEY_A);
    t.release(k, KEY_TAB);
    t.press(k, KEY_TAB);
    t.release(k, KEY_LEFTSHIFT);
    t.send(FromClient::Stop);
    t.assert_let_go();
}

// Found in the third review. Each is ignored, so that the package's checks still pass, and fails
// with cargo test -p linda-keyd -- --ignored fault_ while the fault it names stands.

/// The Menu key, KEY_COMPOSE in the kernel's codes.
const KEY_MENU: u16 = 127;
const KEY_C: u16 = 46;

/// MODIFIERS is nine fixed keys, but which keys change what a later key means is the layout's to
/// say, as Linda's own tables have it (levelShift and keyMods in kernel.py). XKB's options
/// ctrl:menu_rctrl, altwin:menu_win and lv3:menu_switch make the Menu key a Control, a Super or a
/// level three shift; lv3:lsgt_switch and lv5:lsgt_switch make the key left of Z (KEY_102ND) a level
/// shift, and the Neo layouts make it and the key left of Enter (KEY_BACKSLASH) their level three
/// and four shifts. The release of such a key goes out ahead of a press read while it was held
/// whenever that press is still waiting for its verdict, which every press does for the round trip
/// to the client, up to VERDICT_WAIT_TIMEOUT with WAIT: Control+C typed as Menu and C, with Menu let
/// go of before the client has answered C, reaches the program as a plain c, while Linda, told the
/// keys in the order read, takes it for Control+C. No WAIT is needed, only a client that answers C
/// after Menu's release has been read.
#[test]
#[ignore = "third review: a key the layout makes a modifier comes up ahead of a key pressed under it"]
fn fault_a_key_the_layout_makes_a_modifier_does_not_come_up_ahead_of_a_key_pressed_under_it() {
    let (mut t, k) = T::capturing();
    t.pressed_out(k, KEY_MENU);
    t.press(k, KEY_C);
    let c = t.last_seq();
    t.release(k, KEY_MENU);
    t.verdict(c, false);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_MENU, 1), (KEY_C, 1), (KEY_MENU, 0)],
        "Menu, which ctrl:menu_rctrl makes a Control, came up on the virtual keyboard before C, \
         pressed while Menu was held, went down: the program gets c, not Control+C"
    );
}

/// The kernel's EVIOCGKEY, as the mock does not have it: asking which keys are down also empties
/// the open file's queue of every key event not yet read (__evdev_flush_queue, called from
/// evdev_handle_get_val in drivers/input/evdev.c, since Linux 3.12). A key in `flushed` stands for
/// one let go of after the helper's last read and before it asked: the kernel has it up, and its
/// release went with the queue.
struct FlushingDevices {
    inner: MockDevices,
    flushed: std::sync::Arc<std::sync::Mutex<KeySet>>,
}

struct FlushingKeyboard {
    inner: crate::mock::MockKeyboard,
    flushed: std::sync::Arc<std::sync::Mutex<KeySet>>,
}

impl Devices for FlushingDevices {
    type Keyboard = FlushingKeyboard;
    type Virtual = <MockDevices as Devices>::Virtual;

    fn scan(&mut self) -> std::io::Result<Vec<Found>> {
        self.inner.scan()
    }

    fn open(&mut self, found: &Found) -> std::io::Result<FlushingKeyboard> {
        Ok(FlushingKeyboard {
            inner: self.inner.open(found)?,
            flushed: self.flushed.clone(),
        })
    }

    fn create_virtual(&mut self) -> std::io::Result<Self::Virtual> {
        self.inner.create_virtual()
    }

    fn watch(&mut self, on: bool) -> std::io::Result<()> {
        self.inner.watch(on)
    }

    fn watch_fd(&self) -> Option<BorrowedFd<'_>> {
        self.inner.watch_fd()
    }

    fn watch_drain(&mut self) -> bool {
        self.inner.watch_drain()
    }

    fn guard(&self) -> std::sync::Arc<dyn crate::device::Guard> {
        self.inner.guard()
    }
}

impl Keyboard for FlushingKeyboard {
    fn fd(&self) -> BorrowedFd<'_> {
        self.inner.fd()
    }

    fn read(&mut self, out: &mut Vec<InputEvent>) -> Result<(), ReadError> {
        self.inner.read(out)
    }

    fn keys_down(&self) -> std::io::Result<KeySet> {
        let mut down = self.inner.keys_down()?;
        for code in self.flushed.lock().unwrap().iter() {
            down.remove(code);
        }
        Ok(down)
    }

    fn grab(&mut self) -> Result<(), GrabError> {
        self.inner.grab()
    }

    fn ungrab(&mut self) -> bool {
        self.inner.ungrab()
    }

    fn leds(&self) -> std::io::Result<u16> {
        self.inner.leds()
    }

    fn set_leds(&mut self, leds: u16) -> std::io::Result<()> {
        self.inner.set_leds(leds)
    }
}

/// After a loss of events the helper reads everything the kernel has queued, and only at the report
/// that ends the loss asks which keys are down; it then goes on with what it read after that report
/// as though it came after the answer. It came before: the answer is the kernel's state at the
/// question, and the question emptied the queue of every key event not yet read. A key pressed after
/// the loss, its press read in that batch, and let go of before the question, has its release thrown
/// away by the kernel and its press put out by the helper: it stays down on the virtual keyboard,
/// and the desktop repeats it, Tab moving the focus on and on, until the key is pressed and let go
/// of again. The question comes microseconds after the read unless the helper is taken off the
/// processor in between, as on the starved machine that made it lose events.
#[test]
fn fault_a_key_let_go_of_as_the_helper_catches_up_after_lost_events_does_not_stay_down() {
    fn say(core: &mut Core<FlushingDevices>, m: FromClient, now: Instant) {
        core.received(CLIENT, &m.encode(), now);
    }
    let w = World::new();
    let flushed = std::sync::Arc::new(std::sync::Mutex::new(KeySet::new()));
    let mut core = Core::new(FlushingDevices {
        inner: w.devices(),
        flushed: flushed.clone(),
    });
    let mut now = Instant::now();
    let k = w.plug("AT Translated Set 2 keyboard");
    core.front_changed(front(SESSION, 1000), now);
    core.connected(CLIENT, PEER, now);
    let hello = FromClient::Hello {
        version: PROTOCOL_VERSION,
        features: 0,
    };
    say(&mut core, hello, now);
    core.admitted(CLIENT, Ok(SESSION.into()), now);
    say(&mut core, FromClient::Start, now);
    for _ in 0..15 {
        now += ms(100);
        say(&mut core, FromClient::Heartbeat { counter: 0 }, now);
        core.tick(now);
    }
    assert!(w.grabbed(k), "{:?}", core.take_output());
    core.take_output();
    // The helper fell behind and the kernel dropped what it held. Tab goes down after that, its
    // press read with the rest, and comes up before the helper asks which keys are down.
    w.overflow(k, &[]);
    w.press(k, KEY_TAB);
    flushed.lock().unwrap().insert(KEY_TAB);
    core.keyboard_ready(k, now);
    let tab = core.take_output().into_iter().find_map(|o| match o {
        Output::Send(_, ToClient::Key(key)) if key.code == KEY_TAB && key.value == KEY_PRESS => {
            Some(key.seq)
        }
        _ => None,
    });
    // Since the fifth wave, what is read after a loss is left alone, Tab's press with it.
    if let Some(tab) = tab {
        say(
            &mut core,
            FromClient::Verdict {
                seq: tab,
                keep: false,
            },
            now,
        );
    }
    for _ in 0..10 {
        now += ms(100);
        say(&mut core, FromClient::Heartbeat { counter: 0 }, now);
        core.tick(now);
    }
    assert!(
        !w.virtual_down().contains(KEY_TAB),
        "Tab, up on the keyboard for a second, is still down on the virtual keyboard, where the \
         desktop repeats it: the helper put out its press, read before it asked which keys were \
         down, and the kernel threw its release away with the queue as it answered"
    );
}

/// A whole-process freeze is measured by the loop from the later of its poll's start and the
/// deadline it polled for (frozen_for in server.rs), and with a press waiting that deadline is the
/// press's bound: forgiving that much moves the bound to the very moment of the thaw, so the first
/// turn after it passes the press on, before the client, thawed at the same moment, has answered.
/// The freeze is forgiven and the press is not: a key read in the tenth of a second before a freeze
/// that logind did not announce, an NVDA command among them, reaches the program as well as NVDA.
/// The core's own test of a freeze credits the whole frozen time from the press, which the loop
/// never does.
#[test]
fn fault_a_press_read_just_before_a_forgiven_freeze_is_not_passed_on_as_the_freeze_ends() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_INSERT);
    let insert = t.last_seq();
    // The loop polls until its next deadline, Insert's bound; every process stops a millisecond
    // after the read and goes on three seconds later, when the poll comes back late by what
    // Server::turn measures.
    let woke = t.now + Duration::from_secs(3);
    assert!(t.core.thawed(woke), "a first freeze is forgiven");
    t.now = woke;
    t.core.tick(t.now);
    t.take();
    // The client, thawed with the helper, keeps it a moment later.
    t.now += ms(5);
    t.verdict(insert, true);
    assert!(
        !t.w.emitted().contains(&(KEY_INSERT, 1)),
        "Insert, read a millisecond before a freeze the helper forgave, was passed on at the thaw \
         before the client, frozen too, could answer: {:?}",
        t.w.emitted()
    );
}

// Found in the fourth review. Each is ignored, so that the package's checks still pass, and fails
// with cargo test -p linda-keyd -- --ignored fault_ while the fault it names stands.

const KEY_W: u16 = 17;

/// After a loss of events the helper now deals with the whole batch before it asks the kernel which
/// keys are down, but it deals with the keys read after the report that ends the loss as though
/// every key it had down before the loss were down still. A key let go of in the lost stretch stays
/// down, for the client and on the virtual keyboard, while every key read after the report is
/// decided and put out, and its release comes only behind them. Control let go of unseen turns the
/// letters typed next into shortcuts, W into Control+W, which closes a tab; the NVDA key let go of
/// unseen makes Linda take each letter for an NVDA command, Q for the one that quits. The third
/// wave asked at the report and had these releases in line ahead of the keys read after it. A loss
/// needs the helper not to read for the time the kernel's 64 events take to fill, under a second
/// with a key repeating, as on a machine starved of the processor or across a forgiven freeze.
#[test]
fn fault_a_modifier_let_go_of_while_events_were_lost_is_up_before_the_keys_read_after_the_loss() {
    let (mut t, k) = T::capturing();
    t.pressed_out(k, KEY_LEFTCTRL);
    t.keys();
    // The helper falls behind, Control comes up, and the kernel drops what it held. W goes down
    // after the loss and is read with the report that ends it.
    t.w.overflow(k, &[KEY_LEFTCTRL]);
    t.w.press(k, KEY_W);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let keys = t.keys();
    let told: Vec<(u16, u8)> = keys.iter().map(|k| (k.code, k.value)).collect();
    // Since the fifth wave W, read with the loss and held when the kernel is asked, is unseen and
    // lost; it must not be told or put out under Control in any case.
    if let Some(w) = keys
        .iter()
        .find(|k| k.code == KEY_W && k.value == KEY_PRESS)
    {
        t.verdict(w.seq, false);
    }
    t.pass(VERDICT_TIMEOUT);
    let emitted = t.w.emitted();
    let up = emitted.iter().position(|e| *e == (KEY_LEFTCTRL, 0));
    let down = emitted.iter().position(|e| *e == (KEY_W, 1));
    assert!(
        matches!((up, down), (Some(up), down) if down.is_none_or(|d| up < d)),
        "Control, let go of while events were lost, was still down on the virtual keyboard when W, \
         pressed after it, went out, so the program had Control+W: {emitted:?}"
    );
    assert_eq!(
        told.first(),
        Some(&(KEY_LEFTCTRL, KEY_RELEASE)),
        "the client was told of W's press while Control was still down for it: {told:?}"
    );
}

/// The same stale view counts towards the emergency chord: Backspace let go of while events were
/// lost, then Escape and Enter rolled together and read after the report, make the helper let go of
/// every keyboard and drop the keys waiting, though Backspace has been up all the while. Kernel
/// capture stays off until the user asks for it again.
#[test]
fn fault_a_key_let_go_of_while_events_were_lost_does_not_complete_the_emergency_chord() {
    let (mut t, k) = T::capturing();
    t.pressed_out(k, KEY_BACKSPACE);
    t.w.overflow(k, &[KEY_BACKSPACE]);
    t.w.press(k, KEY_ESC);
    t.w.press(k, KEY_ENTER);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let sent = t.sent();
    assert!(
        !sent.contains(&released(Released::Emergency)),
        "only Escape and Enter were down, Backspace having come up while events were lost, and the \
         helper let go as for the emergency chord: {sent:?}"
    );
}

/// The mocked devices, with what the virtual keyboard holds down noted at each ungrab.
struct ProbeDevices {
    inner: MockDevices,
    world: World,
    at_ungrab: std::sync::Arc<std::sync::Mutex<Vec<KeySet>>>,
}

struct ProbeKeyboard {
    inner: crate::mock::MockKeyboard,
    world: World,
    at_ungrab: std::sync::Arc<std::sync::Mutex<Vec<KeySet>>>,
}

impl Devices for ProbeDevices {
    type Keyboard = ProbeKeyboard;
    type Virtual = <MockDevices as Devices>::Virtual;

    fn scan(&mut self) -> std::io::Result<Vec<Found>> {
        self.inner.scan()
    }

    fn open(&mut self, found: &Found) -> std::io::Result<ProbeKeyboard> {
        Ok(ProbeKeyboard {
            inner: self.inner.open(found)?,
            world: self.world.clone(),
            at_ungrab: self.at_ungrab.clone(),
        })
    }

    fn create_virtual(&mut self) -> std::io::Result<Self::Virtual> {
        self.inner.create_virtual()
    }

    fn watch(&mut self, on: bool) -> std::io::Result<()> {
        self.inner.watch(on)
    }

    fn watch_fd(&self) -> Option<BorrowedFd<'_>> {
        self.inner.watch_fd()
    }

    fn watch_drain(&mut self) -> bool {
        self.inner.watch_drain()
    }

    fn guard(&self) -> std::sync::Arc<dyn crate::device::Guard> {
        self.inner.guard()
    }
}

impl Keyboard for ProbeKeyboard {
    fn fd(&self) -> BorrowedFd<'_> {
        self.inner.fd()
    }

    fn read(&mut self, out: &mut Vec<InputEvent>) -> Result<(), ReadError> {
        self.inner.read(out)
    }

    fn keys_down(&self) -> std::io::Result<KeySet> {
        self.inner.keys_down()
    }

    fn grab(&mut self) -> Result<(), GrabError> {
        self.inner.grab()
    }

    fn ungrab(&mut self) -> bool {
        self.at_ungrab
            .lock()
            .unwrap()
            .push(self.world.virtual_down());
        self.inner.ungrab()
    }

    fn leds(&self) -> std::io::Result<u16> {
        self.inner.leds()
    }

    fn set_leds(&mut self, leds: u16) -> std::io::Result<()> {
        self.inner.set_leds(leds)
    }
}

/// Before this wave every key the virtual keyboard held down was released before the first
/// keyboard was ungrabbed. Now each keyboard is read and ungrabbed first, and the releases come
/// after the last ungrab, each of which waits in the kernel for two RCU grace periods. Meanwhile
/// the desktop goes on taking the keys held there as held: Backspace, the emergency chord's first
/// key, which went to the program, is repeated by the desktop once the ungrabs take longer than
/// what was left of its repeat delay, deleting text, and a key let go of on a keyboard already
/// ungrabbed stays down for the desktop until the last is, its release from the keyboard itself
/// ignored as one of a key the desktop never had down from it.
#[test]
fn fault_nothing_is_held_on_the_virtual_keyboard_while_the_keyboards_are_ungrabbed() {
    let w = World::new();
    let at_ungrab = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let mut core = Core::new(ProbeDevices {
        inner: w.devices(),
        world: w.clone(),
        at_ungrab: at_ungrab.clone(),
    });
    let mut now = Instant::now();
    let k = w.plug("AT Translated Set 2 keyboard");
    let say = |core: &mut Core<ProbeDevices>, m: FromClient, now: Instant| {
        core.received(CLIENT, &m.encode(), now);
    };
    core.front_changed(front(SESSION, 1000), now);
    core.connected(CLIENT, PEER, now);
    let hello = FromClient::Hello {
        version: PROTOCOL_VERSION,
        features: 0,
    };
    say(&mut core, hello, now);
    core.admitted(CLIENT, Ok(SESSION.into()), now);
    say(&mut core, FromClient::Start, now);
    for _ in 0..15 {
        now += ms(100);
        say(&mut core, FromClient::Heartbeat { counter: 0 }, now);
        core.tick(now);
    }
    assert!(w.grabbed(k), "{:?}", core.take_output());
    core.take_output();
    // Backspace goes to the program, then Escape and Enter complete the chord.
    w.press(k, KEY_BACKSPACE);
    core.keyboard_ready(k, now);
    let backspace = core
        .take_output()
        .into_iter()
        .find_map(|o| match o {
            Output::Send(_, ToClient::Key(key)) if key.code == KEY_BACKSPACE => Some(key.seq),
            _ => None,
        })
        .expect("Backspace's press was sent to the client");
    let pass = FromClient::Verdict {
        seq: backspace,
        keep: false,
    };
    say(&mut core, pass, now);
    assert!(w.virtual_down().contains(KEY_BACKSPACE));
    w.press(k, KEY_ESC);
    w.press(k, KEY_ENTER);
    core.keyboard_ready(k, now);
    assert!(!w.any_grabbed(), "the chord let go");
    assert!(w.virtual_down().is_empty());
    let held = at_ungrab.lock().unwrap().clone();
    assert!(!held.is_empty(), "the keyboard was ungrabbed");
    assert!(
        held.iter().all(|h| h.is_empty()),
        "Backspace, passed to the program, was still down on the virtual keyboard as the keyboard \
         was ungrabbed, where the desktop repeats it for as long as the ungrabs take: {held:?}"
    );
}

// Every order of keys around a loss of events, against the mock, which keeps the kernel's queue
// and throws it away as the kernel does when asked which keys are down.

#[derive(Clone, Copy, Debug)]
enum Phys {
    Down(u16),
    Up(u16),
}

/// Every sequence of up to `len` presses and releases of the keys, from all of them up, each key
/// going down at most the number of times given with it.
fn sequences(keys: &[(u16, usize)], len: usize) -> Vec<Vec<Phys>> {
    fn grow(
        keys: &[(u16, usize)],
        len: usize,
        down: &mut KeySet,
        pressed: &mut Vec<usize>,
        seq: &mut Vec<Phys>,
        out: &mut Vec<Vec<Phys>>,
    ) {
        out.push(seq.clone());
        if seq.len() == len {
            return;
        }
        for (i, &(code, most)) in keys.iter().enumerate() {
            if down.contains(code) {
                down.remove(code);
                seq.push(Phys::Up(code));
                grow(keys, len, down, pressed, seq, out);
                seq.pop();
                down.insert(code);
            } else if pressed[i] < most {
                pressed[i] += 1;
                down.insert(code);
                seq.push(Phys::Down(code));
                grow(keys, len, down, pressed, seq, out);
                seq.pop();
                down.remove(code);
                pressed[i] -= 1;
            }
        }
    }
    let mut out = Vec::new();
    grow(
        keys,
        len,
        &mut KeySet::new(),
        &mut vec![0; keys.len()],
        &mut Vec::new(),
        &mut out,
    );
    out
}

/// One sequence of physical key events with a loss: events before `a` read one at a time, those
/// from `a` to `b` lost, the loss marked, those from `b` to `r` read with the mark, those from `r`
/// to `q` coming after that read and before the helper asks which keys are down, which throws them
/// away, and those from `q` on read one at a time; then every key still down let go of. The client
/// passes every press at once. A key that modifies others counts as down for the client, and on
/// the virtual keyboard, when a key is told or put out if and only if it was physically down when
/// that key went down, but for a press the helper made up, which may come in any order with
/// others it made up. Returns whether the emergency chord let go, or what went wrong.
fn around_a_loss(seq: &[Phys], a: usize, b: usize, r: usize, q: usize) -> Result<bool, String> {
    let mut states = Vec::new();
    let mut s = KeySet::new();
    for e in seq {
        match e {
            Phys::Down(c) => s.insert(*c),
            Phys::Up(c) => s.remove(*c),
        }
        states.push(s.clone());
    }
    // The keys physically down just after `code` last went down, at or before event `p`.
    let pressed_at = |code: u16, p: Option<usize>| -> Result<KeySet, String> {
        let p = p.ok_or(format!("{code} out before any key went down"))?;
        (0..=p)
            .rev()
            .find(|j| matches!(seq[*j], Phys::Down(c) if c == code))
            .map(|j| states[j].clone())
            .ok_or(format!("{code} out though it never went down"))
    };
    let (mut t, k) = T::capturing();
    let mut told = KeySet::new();
    let mut made_up = KeySet::new();
    let mut out = KeySet::new();
    let mut emitted = 0;
    let mut fired = false;
    let mut look = |t: &mut T, p: Option<usize>| -> Result<(), String> {
        loop {
            let sent = t.sent();
            if sent.is_empty() {
                break;
            }
            for m in sent {
                match m {
                    ToClient::Key(key) if key.value == KEY_PRESS => {
                        if told.contains(key.code) {
                            return Err(format!("{} told down twice", key.code));
                        }
                        let at = pressed_at(key.code, p)?;
                        if key.flags & KEY_SYNTHETIC != 0 {
                            made_up.insert(key.code);
                        } else {
                            for m in MODIFIERS
                                .into_iter()
                                .chain(READER_MODIFIERS)
                                .filter(|m| *m != key.code)
                            {
                                if told.contains(m) && !at.contains(m) {
                                    return Err(format!(
                                        "{} told under {m}, which was up when it went down",
                                        key.code
                                    ));
                                }
                                if !told.contains(m) && at.contains(m) {
                                    return Err(format!(
                                        "{} told without {m}, which was down when it went down",
                                        key.code
                                    ));
                                }
                            }
                        }
                        told.insert(key.code);
                        t.verdict(key.seq, false);
                    }
                    ToClient::Key(key) if key.value == KEY_RELEASE => {
                        if !told.contains(key.code) {
                            return Err(format!("{} told up, never told down", key.code));
                        }
                        told.remove(key.code);
                        made_up.remove(key.code);
                    }
                    ToClient::Released {
                        reason: Released::Emergency,
                    } => fired = true,
                    _ => {}
                }
            }
        }
        let all = t.w.emitted();
        for &(code, value) in &all[emitted..] {
            if value == 1 {
                if out.contains(code) {
                    return Err(format!("{code} put out twice"));
                }
                let at = pressed_at(code, p)?;
                if !made_up.contains(code) {
                    for m in MODIFIERS
                        .into_iter()
                        .chain(READER_MODIFIERS)
                        .filter(|m| *m != code)
                    {
                        if out.contains(m) && !at.contains(m) {
                            return Err(format!(
                                "{code} put out under {m}, which was up when it went down"
                            ));
                        }
                        if !out.contains(m) && at.contains(m) {
                            return Err(format!(
                                "{code} put out without {m}, which was down when it went down"
                            ));
                        }
                    }
                }
                out.insert(code);
            } else {
                if !out.contains(code) {
                    return Err(format!("{code} released, never put out"));
                }
                out.remove(code);
            }
        }
        emitted = all.len();
        Ok(())
    };
    let read = |t: &mut T, e: Phys| {
        match e {
            Phys::Down(c) => t.w.press(k, c),
            Phys::Up(c) => t.w.release(k, c),
        }
        t.core.keyboard_ready(k, t.now);
        t.take();
    };
    for (i, e) in seq.iter().enumerate().take(a) {
        read(&mut t, *e);
        look(&mut t, Some(i))?;
    }
    let unread = |t: &mut T, e: Phys| match e {
        Phys::Down(c) => t.w.change_unread(k, c, true),
        Phys::Up(c) => t.w.change_unread(k, c, false),
    };
    for e in &seq[a..b] {
        unread(&mut t, *e);
    }
    t.w.overflow(k, &[]);
    for e in &seq[b..r] {
        match e {
            Phys::Down(c) => t.w.press(k, *c),
            Phys::Up(c) => t.w.release(k, *c),
        }
    }
    for e in &seq[r..q] {
        unread(&mut t, *e);
    }
    t.core.keyboard_ready(k, t.now);
    t.take();
    look(&mut t, q.checked_sub(1))?;
    for (i, e) in seq.iter().enumerate().skip(q) {
        read(&mut t, *e);
        look(&mut t, Some(i))?;
    }
    let last = seq.len().checked_sub(1);
    for code in s.iter() {
        read(&mut t, Phys::Up(code));
        look(&mut t, last)?;
    }
    t.pass(VERDICT_TIMEOUT * 2);
    look(&mut t, last)?;
    if !t.w.virtual_down().is_empty() {
        return Err(format!(
            "every key up, and {:?} still down on the virtual keyboard",
            t.w.virtual_down()
        ));
    }
    Ok(fired)
}

/// Every placement of a loss, of the end of the read and of the question in each sequence.
fn every_loss(seqs: &[Vec<Phys>], judge: impl Fn(&[Phys], usize, usize, Result<bool, String>)) {
    for seq in seqs {
        let n = seq.len();
        for a in 0..=n {
            for b in a..=n {
                for r in b..=n {
                    for q in r..=n {
                        judge(seq, a, q, around_a_loss(seq, a, b, r, q));
                    }
                }
            }
        }
    }
}

#[test]
fn no_key_goes_out_under_a_modifier_up_nor_without_one_down_in_any_order_around_a_loss_of_events() {
    let seqs = sequences(&[(KEY_LEFTCTRL, 2), (KEY_INSERT, 1), (KEY_A, 1)], 6);
    every_loss(&seqs, |seq, _, _, r| {
        if let Err(e) = r {
            panic!("{seq:?}: {e}");
        }
    });
}

#[test]
fn the_chord_lets_go_in_any_order_around_a_loss_of_events_when_and_only_when_it_was_held() {
    let chord = CHORD.map(|keys| keys[0]);
    let seqs = sequences(&chord.map(|c| (c, 1)), 6);
    every_loss(&seqs, |seq, a, q, r| {
        let fired = match r {
            Ok(fired) => fired,
            Err(e) => panic!("{seq:?}: {e}"),
        };
        let mut s = KeySet::new();
        let together: Vec<bool> = seq
            .iter()
            .map(|e| {
                match e {
                    Phys::Down(c) => s.insert(*c),
                    Phys::Up(c) => s.remove(*c),
                }
                chord.iter().all(|c| s.contains(*c))
            })
            .collect();
        let must = together[..a].iter().any(|t| *t)
            || q.checked_sub(1).is_some_and(|j| together[j])
            || together[q..].iter().any(|t| *t);
        let never = !together.iter().any(|t| *t);
        assert!(
            !must || fired,
            "{seq:?} lost from {a}, asked at {q}: the chord did not let go"
        );
        assert!(
            !never || !fired,
            "{seq:?} lost from {a}, asked at {q}: the chord let go, never held"
        );
    });
}

// Found in the fifth review. Each is ignored, so that the package's checks still pass, and fails
// with cargo test -p linda-keyd -- --ignored fault_ while the fault it names stands.

const KEY_SPACE: u16 = 57;

/// A modifier that goes down while events are lost, and is still held once the helper has caught
/// up, is unseen: never told nor put out. The keys typed under it next are ordinary presses, told
/// to the client and put out on the virtual keyboard without it, so the program has another key
/// than the one typed, not merely none. Control held across the stall and C typed under it gives
/// the program a bare C, which replaces whatever text is selected, where the person meant to copy
/// it; Shift with Enter, a new line in a Discord message, sends the message half written. The
/// enumeration checks that no key goes out under a modifier that was up when it went down; this is
/// the other half, a key going out without a modifier that was down.
#[test]
fn fault_a_key_typed_under_a_modifier_held_across_a_loss_does_not_go_out_without_it() {
    let (mut t, k) = T::capturing();
    // The helper falls behind and the kernel drops what it held; Control goes down at the end of
    // the stall and is read with the loss.
    t.w.overflow(k, &[]);
    t.w.press(k, KEY_LEFTCTRL);
    t.core.keyboard_ready(k, t.now);
    t.take();
    // Caught up, with Control still held, the person types C to copy.
    t.press(k, KEY_C);
    for key in t.keys() {
        if key.value == KEY_PRESS {
            t.verdict(key.seq, false);
        }
    }
    t.pass(VERDICT_TIMEOUT);
    let emitted = t.w.emitted();
    let c = emitted.iter().position(|e| *e == (KEY_C, 1));
    let control = emitted.iter().position(|e| *e == (KEY_LEFTCTRL, 1));
    assert!(
        c.is_none_or(|c| control.is_some_and(|x| x < c)),
        "Control, held since it went down while events were lost, was up on the virtual keyboard \
         when C, typed under it, went out, so the program had C alone where the person typed \
         Control+C: {emitted:?}"
    );
}

/// The same for the NVDA key: Insert held across a loss of events is never told to the client, so
/// the key typed under it comes to Linda alone. NVDA+Space, which switches browse mode to focus
/// mode, comes as Space, which browse mode binds to activatePosition: it presses the button or
/// follows the link at the cursor. NVDA+Tab, which says where the focus is, moves it instead.
#[test]
fn fault_a_key_typed_under_the_nvda_key_held_across_a_loss_is_not_told_without_it() {
    let (mut t, k) = T::capturing();
    t.w.overflow(k, &[]);
    t.w.press(k, KEY_INSERT);
    t.core.keyboard_ready(k, t.now);
    t.take();
    t.press(k, KEY_SPACE);
    let told: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    let space = told.iter().position(|e| *e == (KEY_SPACE, KEY_PRESS));
    let insert = told.iter().position(|e| *e == (KEY_INSERT, KEY_PRESS));
    assert!(
        space.is_none_or(|s| insert.is_some_and(|i| i < s)),
        "Insert, the NVDA key, held since it went down while events were lost, was never told to \
         the client, and Space, pressed under it for NVDA+Space, was told alone, which Linda's \
         browse mode takes for activatePosition, pressing whatever is at the cursor: {told:?}"
    );
}
