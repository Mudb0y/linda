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
        self.hello_asking(conn, 0);
    }

    fn hello_asking(&mut self, conn: ConnId, features: u32) {
        self.from(
            conn,
            FromClient::Hello {
                version: PROTOCOL_VERSION,
                features,
            },
        );
    }

    /// Connects, says HELLO, and is admitted to the session in front.
    fn admit(&mut self, conn: ConnId) {
        self.admit_asking(conn, 0);
    }

    /// As admit, asking for features; answers those WELCOME granted.
    fn admit_asking(&mut self, conn: ConnId, features: u32) -> u32 {
        self.connect(conn, PEER);
        self.hello_asking(conn, features);
        assert!(
            self.out.contains(&Output::Admit(conn, PEER)),
            "{:?}",
            self.out
        );
        self.core.admitted(conn, Ok(SESSION.into()), self.now);
        self.take();
        let welcome =
            |o: &Output| matches!(o, Output::Send(c, ToClient::Welcome { .. }) if *c == conn);
        let granted = self.out.iter().find_map(|o| match o {
            Output::Send(c, ToClient::Welcome { features, .. }) if *c == conn => Some(*features),
            _ => None,
        });
        assert!(granted.is_some(), "{:?}", self.out);
        self.out.retain(|o| !welcome(o));
        granted.unwrap_or_default()
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
            features: FEATURE_TYPING | FEATURE_LATE_PASS | FEATURE_VIRTUAL_SEEN | 0x8000,
        },
    );
    t.core.admitted(CLIENT, Ok(SESSION.into()), t.now);
    t.take();
    assert!(matches!(
        t.sent()[0],
        ToClient::Welcome { features, .. }
            if features == FEATURE_TYPING | FEATURE_LATE_PASS | FEATURE_VIRTUAL_SEEN
    ));
}

/// What Linda's X route asks for, as it has since FEATURE_WAIT came, is granted alone, and such a
/// client is never sent a typed key's answer.
#[test]
fn a_client_asking_for_wait_alone_is_granted_nothing_more() {
    let mut t = T::new();
    t.w.plug("keyboard");
    assert_eq!(t.admit_asking(CLIENT, FEATURE_WAIT), FEATURE_WAIT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    t.send(FromClient::Type {
        tag: 1,
        code: KEY_A,
        press: true,
        mode: TypeMode::Inject,
    });
    let sent = t.sent();
    assert_eq!(goodbye_reason(&sent), Some(Goodbye::Malformed), "{sent:?}");
    assert!(!sent.iter().any(|m| matches!(m, ToClient::Typed { .. })));
    t.assert_let_go();
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

// A session on no console: logind gives VTNr 0 when it finds no consoles on seat0, which the
// kernel may still have.

/// Admitted and asked to capture, with logind naming the client's session on no console.
fn on_no_console(consoles: &[u32]) -> (T, u32) {
    let mut t = T::new();
    for c in consoles {
        t.console(*c);
    }
    t.logind(on_console(SESSION, 1000, 0));
    let k = t.w.plug("AT Translated Set 2 keyboard");
    t.admit(CLIENT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    (t, k)
}

#[test]
fn a_session_on_no_console_captures_though_the_kernel_has_consoles() {
    let (mut t, k) = on_no_console(&[1]);
    assert!(t.w.grabbed(k), "{:?}", t.sent());
    let (mut t, k) = on_no_console(&[]);
    assert!(t.w.grabbed(k), "{:?}", t.sent());
    t.console(1);
    assert!(
        t.w.grabbed(k),
        "let go when the kernel first named its console"
    );
}

#[test]
fn a_session_on_no_console_lets_go_when_the_kernel_leaves_its_console_and_takes_back_on_return() {
    let (mut t, k) = on_no_console(&[1]);
    t.out.clear();
    t.console(3);
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    t.assert_let_go();
    // logind, which has no consoles, goes on naming the same session.
    for _ in 0..3 {
        t.pass(SESSION_RECHECK);
        t.logind(on_console(SESSION, 1000, 0));
    }
    assert!(
        !t.w.is_open(k),
        "grabbed again with a text console in front"
    );
    t.console(1);
    assert!(t.w.grabbed(k));
}

#[test]
fn a_session_on_no_console_is_on_the_console_in_front_when_logind_first_names_it() {
    let (mut t, k) = on_no_console(&[1]);
    t.logind(on_console("c2", 1000, 0));
    t.assert_let_go();
    t.console(4);
    t.logind(on_console(SESSION, 1000, 0));
    assert!(t.w.grabbed(k));
    t.console(1);
    t.assert_let_go();
    // A session logind places on a console is judged by that console, as ever.
    t.logind(on_console(SESSION, 1000, 1));
    assert!(t.w.grabbed(k));
    t.logind(on_console(SESSION, 1000, 0));
    assert!(t.w.grabbed(k), "{:?}", t.sent());
    t.console(4);
    t.assert_let_go();
}

#[test]
fn a_session_on_no_console_keeps_its_console_while_logind_falters_on_a_text_console() {
    for between in [Front::Unknown, Front::Nobody] {
        let (mut t, k) = on_no_console(&[1]);
        assert!(t.w.grabbed(k));
        t.console(3);
        t.assert_let_go();
        // logind does not answer in time, or names nobody, then names the same session again.
        t.logind(between.clone());
        t.pass(Duration::from_secs(2));
        t.logind(on_console(SESSION, 1000, 0));
        assert!(
            !t.w.is_open(k),
            "grabbed with text console 3 in front after {between:?}"
        );
        t.console(1);
        for _ in 0..5 {
            t.pass(SESSION_RECHECK);
            t.logind(on_console(SESSION, 1000, 0));
            assert!(
                t.w.grabbed(k),
                "not grabbed back on console 1 after {between:?}"
            );
        }
    }
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

/// systemd-sleep freezes the client's slice, and the kernel freezes the helper only later, as much
/// as sixteen seconds later on a busy machine: the helper, running on, does not drop the frozen
/// client for its silence, nor refuse a caller frozen before its HELLO, and after the wake each
/// has its usual time again.
#[test]
fn a_client_frozen_before_the_helper_going_to_sleep_is_not_dropped_for_its_silence() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_A);
    t.keys();
    t.connect(OTHER, PEER);
    t.core.sleeping(true, t.now);
    t.take();
    assert_eq!(t.sent(), vec![released(Released::Background)]);
    // Let go of, the press waiting went out, and no run of unanswered presses is counted.
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
    t.quiet(Duration::from_secs(16));
    assert!(
        t.sent().is_empty(),
        "the client was told something while frozen"
    );
    assert!(!t.closed(CLIENT) && !t.closed(OTHER));
    // The kernel freezes the helper too, and five seconds later everything wakes, A long let go of.
    t.w.release(k, KEY_A);
    t.now += Duration::from_secs(5);
    assert!(t.core.thawed(t.now));
    t.core.sleeping(false, t.now);
    t.take();
    t.quiet(HEARTBEAT_TIMEOUT - ms(1));
    assert!(!t.closed(CLIENT) && !t.closed(OTHER));
    assert!(t.w.grabbed(k));
    // Silent the usual time after the wake, it is dropped, as is the caller that never said HELLO.
    t.quiet(ms(1));
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Silent));
    assert!(t.closed(OTHER));
}

/// A sleep logind announces and then calls off: the helper holds nothing meanwhile, and once
/// logind says the machine is awake a client still silent is dropped after the usual time.
#[test]
fn a_sleep_called_off_leaves_a_silent_client_its_usual_time_from_then() {
    let (mut t, _k) = T::capturing();
    t.core.sleeping(true, t.now);
    t.take();
    t.quiet(Duration::from_secs(30));
    assert!(!t.closed(CLIENT));
    t.core.sleeping(false, t.now);
    t.take();
    t.quiet(HEARTBEAT_TIMEOUT - ms(1));
    assert!(!t.closed(CLIENT));
    t.quiet(ms(1));
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
fn the_watchdog_destroys_a_virtual_keyboard_holding_a_key_with_no_keyboard_grabbed() {
    use crate::device::Devices;
    use crate::watchdog::{self, Progress};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering::SeqCst};

    let w = World::new();
    let mut devices = w.devices();
    let guard = devices.guard();
    let mut v = devices.create_virtual().unwrap();
    assert!(!guard.holding());
    v.emit(KEY_LEFTSHIFT, 1).unwrap();
    assert!(!w.any_grabbed());
    assert!(
        guard.holding(),
        "a key down on the virtual keyboard is not counted"
    );
    v.emit(KEY_LEFTSHIFT, 0).unwrap();
    assert!(!guard.holding());
    v.emit(KEY_LEFTSHIFT, 1).unwrap();
    let progress = Progress::new();
    let stopped = Instant::now();
    progress.turned(stopped, None);
    let ended = Arc::new(AtomicBool::new(false));
    let e = ended.clone();
    watchdog::spawn(
        progress,
        guard.clone(),
        Box::new(move || e.store(true, SeqCst)),
    )
    .unwrap()
    .join()
    .unwrap();
    assert!(ended.load(SeqCst));
    assert!(stopped.elapsed() > STALL_LIMIT);
    assert!(w.released_by_guard());
    assert!(w.virtual_down().is_empty());
    assert_eq!(
        w.emitted(),
        vec![
            (KEY_LEFTSHIFT, 1),
            (KEY_LEFTSHIFT, 0),
            (KEY_LEFTSHIFT, 1),
            (KEY_LEFTSHIFT, 0)
        ]
    );
    assert!(!guard.holding());
    assert!(
        v.emit(KEY_A, 1).is_err(),
        "a destroyed virtual keyboard still takes keys"
    );
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
const KEY_LEFTALT: u16 = 56;
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

impl T {
    /// After a loss with `keys` down and unread, the presses the helper made up, all passed.
    fn made_up_and_passed(&mut self, k: u32, keys: &[u16]) -> Vec<Key> {
        for c in keys {
            self.w.change_unread(k, *c, true);
        }
        self.w.overflow(k, &[]);
        self.core.keyboard_ready(k, self.now);
        self.take();
        let told = self.keys();
        for key in &told {
            self.verdict(key.seq, false);
        }
        told
    }
}

/// Alt+F4 typed during a stall, F4 lost and Alt let go of after: the program gets no bare Alt,
/// which would move the focus to the menu bar. The client is told both.
#[test]
fn a_made_up_modifier_let_go_of_before_anything_goes_out_under_it_never_goes_out() {
    let (mut t, k) = T::capturing();
    let told = t.made_up_and_passed(k, &[KEY_LEFTALT]);
    assert_eq!(told.len(), 1);
    t.release(k, KEY_LEFTALT);
    let told: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    assert_eq!(told, vec![(KEY_LEFTALT, KEY_RELEASE)]);
    t.pass(VERDICT_TIMEOUT);
    assert_eq!(t.w.emitted(), vec![]);
    let seq = {
        t.press(k, KEY_A);
        t.last_seq()
    };
    t.verdict(seq, false);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
}

/// Held back, Shift and Control go out in the order told, rising by code, just before the first
/// press that goes out, and stay down for the keys after it; held back as the helper lets go,
/// they never go out at all.
#[test]
fn made_up_modifiers_go_out_together_before_the_first_key_under_them() {
    let (mut t, k) = T::capturing();
    t.made_up_and_passed(k, &[KEY_LEFTSHIFT, KEY_LEFTCTRL]);
    assert_eq!(t.w.emitted(), vec![]);
    // A kept press sends out nothing, so they wait on.
    t.press(k, KEY_G);
    let g = t.last_seq();
    t.verdict(g, true);
    assert_eq!(t.w.emitted(), vec![]);
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.verdict(a, false);
    t.press(k, KEY_C);
    let c = t.last_seq();
    t.verdict(c, false);
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_LEFTCTRL, 1),
            (KEY_LEFTSHIFT, 1),
            (KEY_A, 1),
            (KEY_C, 1)
        ]
    );

    let (mut t, k) = T::capturing();
    t.made_up_and_passed(k, &[KEY_LEFTALT]);
    t.w.press(k, KEY_TAB);
    t.send(FromClient::Stop);
    t.assert_let_go();
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1), (KEY_TAB, 0)]);
}

/// Caps Lock found down goes out when passed, not held back: its press is what toggles the lock,
/// which the light then follows from the desktop, as for a Caps Lock typed by hand.
#[test]
fn a_made_up_caps_lock_passed_goes_out_at_once() {
    let (mut t, k) = T::capturing();
    t.made_up_and_passed(k, &[KEY_CAPSLOCK]);
    assert_eq!(t.w.emitted(), vec![(KEY_CAPSLOCK, 1)]);
    t.release(k, KEY_CAPSLOCK);
    assert_eq!(t.w.emitted(), vec![(KEY_CAPSLOCK, 1), (KEY_CAPSLOCK, 0)]);
}

/// Insert and the numpad's Insert found down are told, for the screen reader, and never put out,
/// whatever the verdict, nor their release; with Num Lock on the numpad's is 0, and stays unseen.
#[test]
fn a_made_up_insert_is_told_and_never_put_out() {
    let (mut t, k) = T::capturing();
    let told: Vec<(u16, u8, u8)> = t
        .made_up_and_passed(k, &[KEY_INSERT, KEY_KP0])
        .iter()
        .map(|k| (k.code, k.value, k.flags))
        .collect();
    assert_eq!(
        told,
        vec![
            (KEY_KP0, KEY_PRESS, KEY_ASK | KEY_SYNTHETIC),
            (KEY_INSERT, KEY_PRESS, KEY_ASK | KEY_SYNTHETIC),
        ]
    );
    t.press(k, KEY_A);
    let a = t.last_seq();
    t.verdict(a, false);
    t.release(k, KEY_INSERT);
    t.release(k, KEY_KP0);
    let told: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    assert_eq!(
        told,
        vec![(KEY_INSERT, KEY_RELEASE), (KEY_KP0, KEY_RELEASE)]
    );
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);

    let (mut t, k) = T::capturing();
    t.desktop_leds(LOCK_NUM);
    t.sent();
    let told: Vec<u16> = t
        .made_up_and_passed(k, &[KEY_KP0, KEY_INSERT])
        .iter()
        .map(|k| k.code)
        .collect();
    assert_eq!(told, vec![KEY_INSERT]);
}

/// A made-up press nobody answers is passed at its bound with EXPIRED, as any press, and a
/// made-up modifier so passed goes out with the first key under it.
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
    t.press(k, KEY_C);
    assert_eq!(t.w.emitted(), vec![]);
    t.pass(VERDICT_TIMEOUT);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTCTRL, 1), (KEY_C, 1)]);
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
/// away, and those from `q` on read one at a time, or in one batch if `batched`, so that the chord
/// can complete in the middle of a batch whose rest the helper passes on as it lets go; then every
/// key still down let go of. The client passes every press at once. A key that modifies others
/// counts as down for the client, and on the virtual keyboard, when a key is told or put out if
/// and only if it was physically down when that key went down, but for a press the helper made
/// up, which may come in any order with others it made up. Returns whether the emergency chord
/// let go, or what went wrong.
fn around_a_loss(
    seq: &[Phys],
    a: usize,
    b: usize,
    r: usize,
    q: usize,
    batched: bool,
) -> Result<bool, String> {
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
    // Keys out on the virtual keyboard that a press has gone out under since.
    let mut under = KeySet::new();
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
                        // When a made-up press went down, against the keys told down, cannot be
                        // known; it is told as made up.
                        if key.flags & KEY_SYNTHETIC != 0 {
                            made_up.insert(key.code);
                        } else {
                            made_up.remove(key.code);
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
                if made_up.contains(code) && READER_MODIFIERS.contains(&code) {
                    return Err(format!("{code}, made up, put out"));
                }
                // To a program only the nine are modifiers. A made-up one goes out just before
                // the first key under it, and is judged by that key.
                for m in MODIFIERS
                    .into_iter()
                    .filter(|m| *m != code && !made_up.contains(code))
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
                for c in out.iter() {
                    under.insert(c);
                }
                out.insert(code);
            } else {
                if !out.contains(code) {
                    return Err(format!("{code} released, never put out"));
                }
                if made_up.contains(code) && !under.contains(code) && code != KEY_CAPSLOCK {
                    return Err(format!("{code}, made up, put out alone"));
                }
                out.remove(code);
                under.remove(code);
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
    if batched && q < seq.len() {
        for e in &seq[q..] {
            match e {
                Phys::Down(c) => t.w.press(k, *c),
                Phys::Up(c) => t.w.release(k, *c),
            }
        }
        t.core.keyboard_ready(k, t.now);
        t.take();
        look(&mut t, Some(seq.len() - 1))?;
    } else {
        for (i, e) in seq.iter().enumerate().skip(q) {
            read(&mut t, *e);
            look(&mut t, Some(i))?;
        }
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
    every_loss_read(seqs, false, judge);
}

fn every_loss_read(
    seqs: &[Vec<Phys>],
    batched: bool,
    judge: impl Fn(&[Phys], usize, usize, Result<bool, String>),
) {
    for seq in seqs {
        let n = seq.len();
        for a in 0..=n {
            for b in a..=n {
                for r in b..=n {
                    for q in r..=n {
                        judge(seq, a, q, around_a_loss(seq, a, b, r, q, batched));
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
fn modifiers_found_down_after_a_loss_go_out_before_what_is_typed_under_them_and_never_alone() {
    let seqs = sequences(
        &[
            (KEY_LEFTCTRL, 1),
            (KEY_LEFTSHIFT, 1),
            (KEY_INSERT, 1),
            (KEY_A, 1),
        ],
        5,
    );
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
    // Read one at a time after the question, and in one batch, whose rest the helper passes on as
    // it lets go.
    for batched in [false, true] {
        every_loss_read(&seqs, batched, |seq, a, q, r| {
            judge_chord(chord, seq, a, q, r)
        });
    }
}

fn judge_chord(chord: [u16; 3], seq: &[Phys], a: usize, q: usize, r: Result<bool, String>) {
    {
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
    }
}

/// Whichever key completes the chord, the numpad's Enter included, it is never told, and nor is
/// its release read in the rest of the batch, nor are the keys after it told but as taps.
#[test]
fn the_key_that_completes_the_chord_is_never_told_down_nor_up() {
    for enter in [KEY_ENTER, KEY_KPENTER] {
        let keys = [KEY_BACKSPACE, KEY_ESC, enter];
        for order in [
            [0, 1, 2],
            [0, 2, 1],
            [1, 0, 2],
            [1, 2, 0],
            [2, 0, 1],
            [2, 1, 0],
        ] {
            let (mut t, k) = T::capturing();
            for i in order {
                t.w.press(k, keys[i]);
            }
            let last = keys[order[2]];
            t.w.release(k, last);
            t.w.press(k, last);
            t.w.release(k, keys[order[0]]);
            t.core.keyboard_ready(k, t.now);
            t.take();
            let told: Vec<(u16, u8, u8)> = t
                .keys()
                .iter()
                .map(|k| (k.code, k.value, k.flags))
                .collect();
            let first = keys[order[0]];
            assert_eq!(
                told,
                vec![
                    (first, KEY_PRESS, KEY_ASK),
                    (keys[order[1]], KEY_PRESS, KEY_ASK),
                    (last, KEY_PRESS, 0),
                    (last, KEY_RELEASE, 0),
                    (first, KEY_RELEASE, 0),
                ],
                "{order:?} with {enter}"
            );
        }
    }
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

// Found in the second pass of the sixth review. Each is ignored, so that the package's checks
// still pass, and fails with cargo test -p linda-keyd -- --ignored fault_ while its fault stands.

/// Insert and the numpad's Insert modify other keys only for the screen reader; to a program they
/// are keys like any other. A made-up press of one that the client passes, as it does when Insert
/// is not one of the user's NVDA keys, or for the numpad's with Num Lock on, where it is 0, is
/// typed late on the virtual keyboard, after every made-up modifier, whichever went down first.
/// The kernel's answer cannot tell Insert pressed before Shift from Insert pressed after it, so one
/// of the two orders comes out wrong whatever order the helper chooses: Insert then Shift gives the
/// program Shift+Insert, which pastes, and Control+Insert copies. The keys that are keys to a
/// program stay unseen for this very reason; the enumeration exempts made-up presses from its
/// ordering checks, and so cannot see it.
#[test]
fn fault_a_made_up_insert_is_not_typed_under_modifiers_it_did_not_go_down_under() {
    for insert_first in [true, false] {
        let (mut t, k) = T::capturing();
        let (first, second) = if insert_first {
            (KEY_INSERT, KEY_LEFTSHIFT)
        } else {
            (KEY_LEFTSHIFT, KEY_INSERT)
        };
        // Both go down while events are lost, and are held when the helper asks.
        t.w.change_unread(k, first, true);
        t.w.change_unread(k, second, true);
        t.w.overflow(k, &[]);
        t.core.keyboard_ready(k, t.now);
        t.take();
        // A client for which Insert is not an NVDA key passes both.
        for key in t.keys() {
            if key.value == KEY_PRESS {
                t.verdict(key.seq, false);
            }
        }
        let emitted = t.w.emitted();
        let shift = emitted.iter().position(|e| *e == (KEY_LEFTSHIFT, 1));
        if let Some(i) = emitted.iter().position(|e| *e == (KEY_INSERT, 1)) {
            let under_shift = shift.is_some_and(|s| s < i);
            assert_eq!(
                under_shift,
                !insert_first,
                "Insert went down {} Shift while events were lost, both held when the helper asked, \
                 and the client passed both; Insert went out on the virtual keyboard {} Shift, so \
                 the program had {}: {emitted:?}",
                if insert_first { "before" } else { "after" },
                if under_shift { "under" } else { "without" },
                if under_shift {
                    "Shift+Insert, which pastes"
                } else {
                    "Insert alone, which toggles overwriting"
                }
            );
        }
    }
}

/// A key told down, let go of and pressed again in what its keyboard has for the helper as it lets
/// go: the release is neither tapped nor told, and the second press is told as a tap, a press of a
/// key the client was told is down and never told is up. Linda takes such a press for one of the
/// key held on another keyboard, which reaches neither NVDA nor the program, so NVDA hears nothing
/// of the very key the tap was told for.
#[test]
fn fault_a_key_tapped_as_the_helper_lets_go_is_not_told_down_twice() {
    let (mut t, k) = T::capturing();
    t.pressed_out(k, KEY_A);
    t.keys();
    t.w.release(k, KEY_A);
    t.w.press(k, KEY_A);
    t.send(FromClient::Stop);
    let told: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    let press = told.iter().position(|e| *e == (KEY_A, KEY_PRESS));
    let release = told.iter().position(|e| *e == (KEY_A, KEY_RELEASE));
    assert!(
        press.is_none_or(|p| release.is_some_and(|r| r < p)),
        "A, told down and not told up, was told pressed again as a tap as the helper let go: {told:?}"
    );
}

// Found in the third pass of the sixth review. Ignored, so that the package's checks still pass,
// and fails with cargo test -p linda-keyd -- --ignored fault_ while its fault stands.

/// The emergency chord's last key goes among the keyboard's keys down before the chord is looked
/// for, and when the chord lets go it is never told to the client. As the helper lets go, the
/// release of every key among them read there is told, so this key's release is told too: a
/// release of a key the client was never told was down.
#[test]
fn fault_the_chords_last_key_is_not_told_up_without_being_told_down() {
    let (mut t, k) = T::capturing();
    for c in CHORD.map(|keys| keys[0]) {
        t.w.press(k, c);
    }
    t.w.release(k, KEY_ENTER);
    t.core.keyboard_ready(k, t.now);
    t.take();
    let told: Vec<(u16, u8)> = t.keys().iter().map(|k| (k.code, k.value)).collect();
    assert!(
        told.contains(&(KEY_ENTER, KEY_PRESS)) || !told.contains(&(KEY_ENTER, KEY_RELEASE)),
        "Enter completed the emergency chord and was never told down, and its release, read in the \
         rest of the batch, was told: {told:?}"
    );
}

// Typing, with FEATURE_TYPING.

const KEY_B: u16 = 48;

impl T {
    /// As capturing, with FEATURE_TYPING asked for and granted.
    fn typing() -> (T, u32) {
        T::typing_asking(FEATURE_TYPING)
    }

    fn typing_asking(features: u32) -> (T, u32) {
        let mut t = T::new();
        let k = t.w.plug("AT Translated Set 2 keyboard");
        assert_eq!(t.admit_asking(CLIENT, features), features);
        t.send(FromClient::Start);
        t.pass(VIRTUAL_SETTLE);
        assert!(t.w.grabbed(k), "{:?}", t.sent());
        t.out.clear();
        (t, k)
    }

    fn type_key(&mut self, tag: u32, code: u16, press: bool) {
        self.send(FromClient::Type {
            tag,
            code,
            press,
            mode: TypeMode::Inject,
        });
    }

    /// The TYPED answers since last asked, as tag and outcome, with everything else dropped.
    fn typed(&mut self) -> Vec<(u32, u8)> {
        self.sent()
            .into_iter()
            .filter_map(|m| match m {
                ToClient::Typed { tag, seq, outcome } => {
                    assert_eq!(seq, 0, "an injected key is sent as no KEY");
                    Some((tag, outcome))
                }
                _ => None,
            })
            .collect()
    }

    /// Passes on the press of the key just read.
    fn pass_last(&mut self) {
        let seq = self.last_seq();
        self.verdict(seq, false);
    }
}

/// Neither a press of SysRq while an Alt was down on the virtual keyboard, nor a press of an Alt
/// while SysRq was down there, in what it put out.
fn no_sysrq_with_alt(emitted: &[(u16, i32)]) {
    let mut down = KeySet::new();
    for (code, value) in emitted {
        if *value == 1 {
            let alt = down.contains(KEY_LEFTALT) || down.contains(KEY_RIGHTALT);
            assert!(!(*code == KEY_SYSRQ && alt), "SysRq under Alt: {emitted:?}");
            let alt_key = *code == KEY_LEFTALT || *code == KEY_RIGHTALT;
            assert!(
                !(alt_key && down.contains(KEY_SYSRQ)),
                "Alt over SysRq: {emitted:?}"
            );
            down.insert(*code);
        } else if *value == 0 {
            down.remove(*code);
        }
    }
}

#[test]
fn a_typed_key_goes_out_in_line_behind_the_keys_read_before_it() {
    let (mut t, k) = T::typing();
    t.type_key(1, KEY_A, true);
    assert_eq!(t.typed(), vec![(1, TYPED_OUT)]);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    t.type_key(2, KEY_A, false);
    assert_eq!(t.typed(), vec![(2, TYPED_OUT)]);
    t.press(k, KEY_T);
    let seq = t.last_seq();
    t.type_key(3, KEY_C, true);
    t.type_key(4, KEY_C, false);
    assert_eq!(
        t.typed(),
        vec![],
        "went out before the press read before it"
    );
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
    t.verdict(seq, false);
    assert_eq!(t.typed(), vec![(3, TYPED_OUT), (4, TYPED_OUT)]);
    assert_eq!(
        t.w.emitted()[2..],
        [(KEY_T, 1), (KEY_C, 1), (KEY_C, 0)],
        "{:?}",
        t.w.emitted()
    );
    // No KEY was sent for an injected key, and no number of the client's spent on one.
    t.release(k, KEY_T);
    assert_eq!(
        t.keys().iter().map(|k| k.seq).collect::<Vec<_>>(),
        vec![seq + 1]
    );
    assert!(t.w.virtual_down().is_empty());
}

#[test]
fn type_from_a_client_not_granted_typing_drops_it_and_lets_go() {
    let (mut t, k) = T::capturing();
    t.press(k, KEY_LEFTSHIFT);
    t.pass_last();
    t.type_key(1, KEY_A, true);
    let sent = t.sent();
    assert_eq!(goodbye_reason(&sent), Some(Goodbye::Malformed), "{sent:?}");
    assert!(t.closed(CLIENT));
    t.assert_let_go();
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTSHIFT, 1), (KEY_LEFTSHIFT, 0)]);
}

#[test]
fn typing_is_refused_unless_the_helper_captures_for_the_client_in_front() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.admit_asking(CLIENT, FEATURE_TYPING);
    t.type_key(1, KEY_A, true);
    t.send(FromClient::Start);
    t.type_key(2, KEY_A, true);
    assert_eq!(
        t.typed(),
        vec![(1, TYPED_REFUSED_STATE), (2, TYPED_REFUSED_STATE)],
        "before START and while the virtual keyboard settles"
    );
    t.pass(VIRTUAL_SETTLE);
    assert!(t.w.grabbed(k));
    t.type_key(3, KEY_A, true);
    t.type_key(4, KEY_A, false);
    assert_eq!(t.typed(), vec![(3, TYPED_OUT), (4, TYPED_OUT)]);
    t.send(FromClient::Stop);
    t.type_key(5, KEY_A, true);
    assert_eq!(t.typed(), vec![(5, TYPED_REFUSED_STATE)], "after STOP");
    t.send(FromClient::Start);
    assert!(t.w.grabbed(k));
    t.logind(front("c1", 1000));
    t.type_key(6, KEY_A, true);
    assert_eq!(
        t.typed(),
        vec![(6, TYPED_REFUSED_STATE)],
        "in the background"
    );
    t.logind(front(SESSION, 1000));
    assert!(t.w.grabbed(k));
    t.console(3);
    t.type_key(7, KEY_A, true);
    assert_eq!(
        t.typed(),
        vec![(7, TYPED_REFUSED_STATE)],
        "on another console before logind says so"
    );
    t.console(X_CONSOLE);
    t.logind(front(SESSION, 1000));
    t.core.sleeping(true, t.now);
    t.take();
    t.type_key(8, KEY_A, true);
    assert_eq!(t.typed(), vec![(8, TYPED_REFUSED_STATE)], "asleep");
    t.core.sleeping(false, t.now);
    t.take();
    assert!(t.w.grabbed(k));
    for c in CHORD.map(|keys| keys[0]) {
        t.press(k, c);
    }
    assert!(t.sent().contains(&released(Released::Emergency)));
    t.type_key(9, KEY_A, true);
    assert_eq!(
        t.typed(),
        vec![(9, TYPED_REFUSED_STATE)],
        "after the emergency chord"
    );
    let a: Vec<_> = t.w.emitted().into_iter().filter(|e| e.0 == KEY_A).collect();
    assert_eq!(a, vec![(KEY_A, 1), (KEY_A, 0)]);
    assert!(!t.closed(CLIENT));
}

#[test]
fn typing_needs_no_keyboard_held() {
    // One keyboard another program holds, and none at all.
    for plug in [true, false] {
        let mut t = T::new();
        if plug {
            let k = t.w.plug("keyboard");
            t.w.set_busy(k, true);
        }
        t.admit_asking(CLIENT, FEATURE_TYPING);
        t.send(FromClient::Start);
        t.pass(VIRTUAL_SETTLE);
        assert!(!t.w.any_grabbed());
        t.type_key(1, KEY_A, true);
        t.type_key(2, KEY_A, false);
        assert_eq!(t.typed(), vec![(1, TYPED_OUT), (2, TYPED_OUT)]);
        assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
    }
}

#[test]
fn typed_keys_waiting_as_the_helper_lets_go_are_lost_and_never_go_out() {
    for way in [
        "stop", "console", "logind", "sleep", "chord", "virtual", "gone",
    ] {
        let (mut t, k) = T::typing();
        t.type_key(1, KEY_LEFTSHIFT, true);
        assert_eq!(t.typed(), vec![(1, TYPED_OUT)]);
        t.press(k, KEY_T);
        let seq = t.last_seq();
        t.type_key(2, KEY_A, true);
        t.type_key(3, KEY_LEFTSHIFT, false);
        assert_eq!(t.typed(), vec![], "{way}");
        let reason = match way {
            "stop" => {
                t.send(FromClient::Stop);
                Some(Released::Stopped)
            }
            "console" => {
                t.console(3);
                Some(Released::Background)
            }
            "logind" => {
                t.logind(front("c1", 1000));
                Some(Released::Background)
            }
            "sleep" => {
                t.core.sleeping(true, t.now);
                t.take();
                Some(Released::Background)
            }
            "chord" => {
                for c in CHORD.map(|keys| keys[0]) {
                    t.press(k, c);
                }
                Some(Released::Emergency)
            }
            "virtual" => {
                t.w.fail_emit(true);
                t.verdict(seq, false);
                Some(Released::VirtualKeyboard)
            }
            _ => {
                t.core.disconnected(CLIENT, t.now);
                t.take();
                None
            }
        };
        let sent = t.sent();
        let lost: Vec<usize> = sent
            .iter()
            .enumerate()
            .filter(|(_, m)| {
                matches!(
                    m,
                    ToClient::Typed {
                        outcome: TYPED_LOST,
                        ..
                    }
                )
            })
            .map(|(i, _)| i)
            .collect();
        match reason {
            Some(reason) => {
                let at = sent.iter().position(|m| *m == released(reason));
                assert!(at.is_some(), "{way}: {sent:?}");
                assert_eq!(lost.len(), 2, "{way}: {sent:?}");
                assert!(lost.iter().all(|i| Some(*i) < at), "{way}: {sent:?}");
                assert!(
                    sent.iter().all(
                        |m| !matches!(m, ToClient::Typed { outcome, .. } if *outcome != TYPED_LOST)
                    ),
                    "{way}: {sent:?}"
                );
            }
            None => assert!(sent.is_empty(), "{way}: {sent:?}"),
        }
        assert!(
            t.w.emitted().iter().all(|e| e.0 != KEY_A),
            "{way}: the typed key went out: {:?}",
            t.w.emitted()
        );
        t.assert_let_go();
    }
}

#[test]
fn only_the_keys_linda_knows_are_typed_and_never_a_power_key() {
    let (mut t, _k) = T::typing();
    let refused = [
        KEY_POWER,
        KEY_SLEEP,
        KEY_SUSPEND,
        KEY_POWER2,
        KEY_RESTART,
        0x160,
        KEY_MAX,
    ];
    for (tag, code) in refused.iter().enumerate() {
        t.type_key(tag as u32, *code, true);
    }
    let want: Vec<(u32, u8)> = (0..refused.len() as u32)
        .map(|tag| (tag, TYPED_REFUSED_KEY))
        .collect();
    assert_eq!(t.typed(), want);
    t.type_key(100, KEY_POWER, false);
    assert_eq!(t.typed(), vec![(100, TYPED_NOT_DOWN)]);
    assert!(t.w.emitted().is_empty());
    assert!(!t.closed(CLIENT));
    for code in TYPEABLE {
        t.type_key(u32::from(code), code, true);
        t.type_key(u32::from(code), code, false);
        assert_eq!(
            t.typed(),
            vec![(u32::from(code), TYPED_OUT), (u32::from(code), TYPED_OUT)],
            "{code}"
        );
        // Within the rate.
        t.pass(ms(20));
    }
    assert_eq!(t.w.emitted().len(), 2 * TYPEABLE.len());
    // A code the virtual keyboard cannot send breaks the protocol.
    t.type_key(200, 0x110, true);
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Malformed));
    assert!(t.closed(CLIENT));
    t.assert_let_go();
}

#[test]
fn a_typed_key_goes_down_once_and_up_once() {
    let (mut t, _k) = T::typing();
    t.type_key(1, KEY_A, false);
    t.type_key(2, KEY_A, true);
    t.type_key(3, KEY_A, true);
    t.type_key(4, KEY_A, false);
    t.type_key(5, KEY_A, false);
    assert_eq!(
        t.typed(),
        vec![
            (1, TYPED_NOT_DOWN),
            (2, TYPED_OUT),
            (3, TYPED_REFUSED_KEY),
            (4, TYPED_OUT),
            (5, TYPED_NOT_DOWN)
        ]
    );
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
}

#[test]
fn a_key_typed_down_and_held_on_a_keyboard_is_one_key() {
    let (mut t, k) = T::typing();
    t.press(k, KEY_A);
    t.pass_last();
    t.type_key(1, KEY_A, true);
    assert_eq!(t.typed(), vec![(1, TYPED_OUT)]);
    t.release(k, KEY_A);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    t.type_key(2, KEY_A, false);
    assert_eq!(t.typed(), vec![(2, TYPED_OUT)]);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
}

#[test]
fn a_typed_release_goes_ahead_of_waiting_presses_unless_it_is_a_modifiers() {
    let (mut t, k) = T::typing();
    t.type_key(1, KEY_A, true);
    t.type_key(2, KEY_LEFTSHIFT, true);
    assert_eq!(t.typed(), vec![(1, TYPED_OUT), (2, TYPED_OUT)]);
    t.press(k, KEY_T);
    let seq = t.last_seq();
    t.type_key(3, KEY_A, false);
    t.type_key(4, KEY_LEFTSHIFT, false);
    assert_eq!(t.typed(), vec![(3, TYPED_OUT)]);
    t.verdict(seq, false);
    assert_eq!(t.typed(), vec![(4, TYPED_OUT)]);
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_A, 1),
            (KEY_LEFTSHIFT, 1),
            (KEY_A, 0),
            (KEY_T, 1),
            (KEY_LEFTSHIFT, 0)
        ]
    );
}

#[test]
fn typed_sysrq_is_refused_while_an_alt_is_down_and_a_typed_alt_while_sysrq_is() {
    let (mut t, k) = T::typing();
    for alt in [KEY_LEFTALT, KEY_RIGHTALT] {
        t.press(k, alt);
        t.pass_last();
        t.type_key(1, KEY_SYSRQ, true);
        assert_eq!(t.typed(), vec![(1, TYPED_REFUSED_KEY)], "{alt}");
        t.release(k, alt);
        t.type_key(2, KEY_SYSRQ, true);
        t.type_key(3, KEY_SYSRQ, false);
        assert_eq!(t.typed(), vec![(2, TYPED_OUT), (3, TYPED_OUT)], "{alt}");
    }
    // An Alt typed down counts as one held.
    t.type_key(4, KEY_LEFTALT, true);
    t.type_key(5, KEY_SYSRQ, true);
    t.type_key(6, KEY_LEFTALT, false);
    assert_eq!(
        t.typed(),
        vec![(4, TYPED_OUT), (5, TYPED_REFUSED_KEY), (6, TYPED_OUT)]
    );
    // SysRq held by the user.
    t.press(k, KEY_SYSRQ);
    t.pass_last();
    t.type_key(7, KEY_LEFTALT, true);
    t.type_key(8, KEY_RIGHTALT, true);
    t.type_key(9, KEY_A, true);
    t.type_key(10, KEY_A, false);
    assert_eq!(
        t.typed(),
        vec![
            (7, TYPED_REFUSED_KEY),
            (8, TYPED_REFUSED_KEY),
            (9, TYPED_OUT),
            (10, TYPED_OUT)
        ]
    );
    t.release(k, KEY_SYSRQ);
    t.type_key(11, KEY_LEFTALT, true);
    t.type_key(12, KEY_LEFTALT, false);
    assert_eq!(t.typed(), vec![(11, TYPED_OUT), (12, TYPED_OUT)]);
    no_sysrq_with_alt(&t.w.emitted());
    assert!(t.w.virtual_down().is_empty());
}

#[test]
fn no_typed_press_while_the_kernels_sysrq_handler_may_be_active() {
    let (mut t, k) = T::typing();
    t.type_key(1, KEY_A, true);
    assert_eq!(t.typed(), vec![(1, TYPED_OUT)]);
    // Alt+Print, a screenshot to the desktop and SysRq to the kernel, with both Alts down.
    t.press(k, KEY_LEFTALT);
    t.pass_last();
    t.press(k, KEY_RIGHTALT);
    t.pass_last();
    t.press(k, KEY_SYSRQ);
    t.pass_last();
    t.release(k, KEY_SYSRQ);
    t.type_key(2, KEY_B, true);
    t.type_key(3, KEY_RIGHTALT, true);
    // A release is no command.
    t.type_key(4, KEY_A, false);
    assert_eq!(
        t.typed(),
        vec![
            (2, TYPED_REFUSED_KEY),
            (3, TYPED_REFUSED_KEY),
            (4, TYPED_OUT)
        ]
    );
    t.release(k, KEY_LEFTALT);
    t.type_key(5, KEY_B, true);
    assert_eq!(
        t.typed(),
        vec![(5, TYPED_REFUSED_KEY)],
        "AltGr is still down"
    );
    t.release(k, KEY_RIGHTALT);
    t.type_key(6, KEY_B, true);
    t.type_key(7, KEY_B, false);
    assert_eq!(t.typed(), vec![(6, TYPED_OUT), (7, TYPED_OUT)]);
    let b: Vec<_> = t.w.emitted().into_iter().filter(|e| e.0 == KEY_B).collect();
    assert_eq!(b, vec![(KEY_B, 1), (KEY_B, 0)]);
}

#[test]
fn the_sysrq_rules_are_checked_again_as_the_typed_press_goes_out() {
    let (mut t, k) = T::typing();
    // Nothing is down on the virtual keyboard as these come; Alt goes out ahead of them.
    t.press(k, KEY_LEFTALT);
    let alt = t.last_seq();
    t.type_key(1, KEY_SYSRQ, true);
    t.type_key(2, KEY_SYSRQ, false);
    assert_eq!(t.typed(), vec![]);
    t.verdict(alt, false);
    assert_eq!(t.typed(), vec![(1, TYPED_REFUSED_KEY), (2, TYPED_NOT_DOWN)]);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1)]);
    // The user's SysRq goes out under Alt ahead of a typed key, which the kernel would take as a
    // command.
    t.press(k, KEY_SYSRQ);
    let sysrq = t.last_seq();
    t.type_key(3, KEY_B, true);
    t.type_key(4, KEY_B, false);
    assert_eq!(t.typed(), vec![]);
    t.verdict(sysrq, false);
    assert_eq!(t.typed(), vec![(3, TYPED_REFUSED_KEY), (4, TYPED_NOT_DOWN)]);
    assert!(t.w.emitted().iter().all(|e| e.0 != KEY_B));
    // Refused, the press took no place: the key may be typed again once the handler is done.
    t.release(k, KEY_SYSRQ);
    t.release(k, KEY_LEFTALT);
    t.type_key(5, KEY_B, true);
    t.type_key(6, KEY_B, false);
    assert_eq!(t.typed(), vec![(5, TYPED_OUT), (6, TYPED_OUT)]);
}

#[test]
fn no_typed_press_goes_out_after_the_virtual_keyboard_failed_to_take_an_alt_release() {
    let (mut t, k) = T::typing();
    t.press(k, KEY_LEFTALT);
    t.pass_last();
    t.press(k, KEY_SYSRQ);
    let sysrq = t.last_seq();
    t.release(k, KEY_SYSRQ);
    t.release(k, KEY_LEFTALT);
    t.type_key(1, KEY_B, true);
    assert_eq!(t.typed(), vec![]);
    // The kernel never sees Alt go up, so its SysRq handler stays active and would take b as a
    // command, though the helper counts Alt as up.
    t.w.fail_emit_once(KEY_LEFTALT, 0);
    t.verdict(sysrq, false);
    assert_eq!(t.typed(), vec![(1, TYPED_LOST)]);
    assert!(t.w.emitted().iter().all(|e| e.0 != KEY_B));
}

#[test]
fn a_made_up_alt_held_back_counts_as_down_for_typed_sysrq() {
    let (mut t, k) = T::typing();
    t.made_up_and_passed(k, &[KEY_LEFTALT]);
    t.type_key(1, KEY_SYSRQ, true);
    assert_eq!(t.typed(), vec![(1, TYPED_REFUSED_KEY)]);
    assert_eq!(t.w.emitted(), vec![]);
}

#[test]
fn at_most_sixty_four_typed_keys_wait_in_line() {
    let (mut t, k) = T::typing();
    t.press(k, KEY_T);
    let seq = t.last_seq();
    let codes: Vec<u16> = TYPEABLE
        .iter()
        .copied()
        .filter(|c| ![KEY_T, KEY_SYSRQ, KEY_LEFTALT, KEY_RIGHTALT].contains(c))
        .take(TYPED_WAITING_LIMIT + 1)
        .collect();
    for (tag, code) in codes.iter().enumerate() {
        t.type_key(tag as u32, *code, true);
    }
    assert_eq!(
        t.typed(),
        vec![(TYPED_WAITING_LIMIT as u32, TYPED_REFUSED_FULL)]
    );
    // The release of a key typed down is taken all the same.
    t.type_key(100, codes[0], false);
    assert_eq!(t.typed(), vec![]);
    t.verdict(seq, false);
    let typed = t.typed();
    assert_eq!(typed.len(), TYPED_WAITING_LIMIT + 1, "{typed:?}");
    assert!(typed.iter().all(|(_, o)| *o == TYPED_OUT), "{typed:?}");
    assert_eq!(typed.last(), Some(&(100, TYPED_OUT)));
    for code in &codes[1..TYPED_WAITING_LIMIT] {
        t.type_key(1, *code, false);
    }
    assert_eq!(t.typed().len(), TYPED_WAITING_LIMIT - 1);
    t.release(k, KEY_T);
    assert!(t.w.virtual_down().is_empty());
}

#[test]
fn typing_is_held_to_a_hundred_keys_a_second_in_bursts_of_two_hundred() {
    let (mut t, _k) = T::typing();
    let pairs = |t: &mut T, n: u32| {
        for _ in 0..n {
            t.type_key(1, KEY_A, true);
            t.type_key(2, KEY_A, false);
        }
    };
    pairs(&mut t, TYPED_BURST / 2);
    let typed = t.typed();
    assert_eq!(typed.len(), TYPED_BURST as usize);
    assert!(typed.iter().all(|(_, o)| *o == TYPED_OUT), "{typed:?}");
    t.type_key(3, KEY_A, true);
    assert_eq!(t.typed(), vec![(3, TYPED_REFUSED_FULL)]);
    let per_key = Duration::from_secs(1) / TYPED_PER_SECOND;
    t.pass(per_key);
    t.type_key(4, KEY_A, true);
    t.type_key(5, KEY_B, true);
    // A release of a key typed down is never refused for the rate, the bucket empty.
    t.type_key(6, KEY_A, false);
    assert_eq!(
        t.typed(),
        vec![(4, TYPED_OUT), (5, TYPED_REFUSED_FULL), (6, TYPED_OUT)]
    );
    t.pass(Duration::from_secs(1));
    pairs(&mut t, TYPED_PER_SECOND / 2);
    t.type_key(7, KEY_A, true);
    let typed = t.typed();
    assert_eq!(typed.len(), TYPED_PER_SECOND as usize + 1);
    assert_eq!(typed.last(), Some(&(7, TYPED_REFUSED_FULL)));
    // However long the pause, no more than a burst at once.
    t.pass(Duration::from_secs(10));
    pairs(&mut t, TYPED_BURST / 2);
    t.type_key(8, KEY_A, true);
    let typed = t.typed();
    assert_eq!(typed.len(), TYPED_BURST as usize + 1);
    assert_eq!(typed.last(), Some(&(8, TYPED_REFUSED_FULL)));
    assert!(t.w.virtual_down().is_empty());
}

#[test]
fn typed_keys_count_neither_toward_the_emergency_chord_nor_against_it() {
    let (mut t, k) = T::typing();
    for (tag, c) in [(1, KEY_BACKSPACE), (2, KEY_ESC), (3, KEY_ENTER)] {
        t.type_key(tag, c, true);
    }
    assert_eq!(t.typed().len(), 3);
    t.press(k, KEY_BACKSPACE);
    t.pass_last();
    t.press(k, KEY_ESC);
    t.pass_last();
    assert!(
        !t.sent().contains(&released(Released::Emergency)),
        "typed keys made the chord"
    );
    assert!(t.w.grabbed(k));
    t.press(k, KEY_ENTER);
    assert!(t.sent().contains(&released(Released::Emergency)));
    t.assert_let_go();
}

#[test]
fn injected_keys_do_not_end_a_run_of_unanswered_presses() {
    let (mut t, k) = T::typing();
    let mut elapsed = Duration::ZERO;
    while !t.closed(CLIENT) {
        t.press(k, KEY_T);
        t.pass(ms(50));
        t.type_key(1, KEY_A, true);
        t.type_key(2, KEY_A, false);
        t.pass(ms(100));
        t.release(k, KEY_T);
        t.pass(ms(150));
        elapsed += ms(300);
        assert!(elapsed < Duration::from_secs(3), "never dropped");
    }
    assert!(elapsed >= UNANSWERED_SPAN, "dropped after {elapsed:?}");
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Unanswered));
    t.assert_let_go();
}

// Typing with TYPE_DECIDE: the key comes back as KEY, to be decided as a key read from a keyboard.

impl T {
    fn decide_key(&mut self, tag: u32, code: u16, press: bool) {
        self.send(FromClient::Type {
            tag,
            code,
            press,
            mode: TypeMode::Decide,
        });
    }

    /// The sequence number of the typed key just told, with everything else sent since dropped.
    fn typed_seq(&mut self) -> u64 {
        let keys = self.keys();
        let key = keys.last().expect("a key was sent");
        assert_eq!(key.device, TYPED_DEVICE, "{keys:?}");
        key.seq
    }
}

fn typed_key(seq: u64, code: u16, press: bool) -> ToClient {
    ToClient::Key(Key {
        seq,
        device: TYPED_DEVICE,
        code,
        value: if press { KEY_PRESS } else { KEY_RELEASE },
        flags: if press {
            KEY_ASK | KEY_TYPED
        } else {
            KEY_TYPED
        },
    })
}

fn answered(tag: u32, seq: u64, outcome: u8) -> ToClient {
    ToClient::Typed { tag, seq, outcome }
}

#[test]
fn a_key_typed_to_be_decided_comes_back_as_a_key_and_goes_out_once_passed() {
    let (mut t, _k) = T::typing();
    t.decide_key(1, KEY_A, true);
    let sent = t.sent();
    let [ToClient::Key(Key { seq: a, .. })] = sent[..] else {
        panic!("{sent:?}")
    };
    assert_eq!(sent, vec![typed_key(a, KEY_A, true)]);
    assert!(t.w.emitted().is_empty(), "went out before its verdict");
    t.verdict(a, false);
    assert_eq!(t.sent(), vec![answered(1, a, TYPED_OUT)]);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    t.decide_key(2, KEY_A, false);
    assert_eq!(
        t.sent(),
        vec![
            typed_key(a + 1, KEY_A, false),
            answered(2, a + 1, TYPED_OUT)
        ]
    );
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
    assert!(!t.closed(CLIENT));
}

#[test]
fn a_key_typed_to_be_decided_and_kept_never_goes_out() {
    let (mut t, _k) = T::typing();
    t.decide_key(1, KEY_A, true);
    let a = t.typed_seq();
    t.verdict(a, true);
    assert_eq!(t.sent(), vec![answered(1, a, TYPED_KEPT)]);
    t.decide_key(2, KEY_A, false);
    assert_eq!(
        t.sent(),
        vec![
            typed_key(a + 1, KEY_A, false),
            answered(2, a + 1, TYPED_KEPT)
        ]
    );
    assert!(t.w.emitted().is_empty(), "{:?}", t.w.emitted());
    // Released, it may be typed down again.
    t.decide_key(3, KEY_A, true);
    let again = t.typed_seq();
    t.verdict(again, false);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
}

#[test]
fn a_key_typed_to_be_decided_waits_in_line_with_the_keys_read_around_it() {
    let (mut t, k) = T::typing();
    t.press(k, KEY_T);
    t.decide_key(1, KEY_A, true);
    t.press(k, KEY_B);
    let told: Vec<(u32, u64)> = t.keys().iter().map(|k| (k.device, k.seq)).collect();
    let [(_, user), (TYPED_DEVICE, a), (_, b)] = told[..] else {
        panic!("{told:?}")
    };
    t.verdict(b, false);
    t.verdict(a, false);
    assert!(
        t.w.emitted().is_empty(),
        "went out before the press read before it"
    );
    assert!(t.sent().is_empty());
    t.verdict(user, false);
    assert_eq!(t.w.emitted(), vec![(KEY_T, 1), (KEY_A, 1), (KEY_B, 1)]);
    assert_eq!(t.sent(), vec![answered(1, a, TYPED_OUT)]);
    // Its release, typed while a press waits, goes ahead of that press as any release does, and a
    // modifier typed after that press waits behind it.
    t.press(k, KEY_LEFTSHIFT);
    let shift = t.last_seq();
    t.decide_key(2, KEY_A, false);
    assert_eq!(
        t.sent(),
        vec![
            typed_key(shift + 1, KEY_A, false),
            answered(2, shift + 1, TYPED_OUT)
        ]
    );
    t.decide_key(3, KEY_LEFTALT, true);
    let alt = t.typed_seq();
    t.verdict(alt, false);
    assert!(t.sent().is_empty());
    t.verdict(shift, false);
    assert_eq!(
        t.w.emitted()[3..],
        [(KEY_A, 0), (KEY_LEFTSHIFT, 1), (KEY_LEFTALT, 1)],
        "{:?}",
        t.w.emitted()
    );
    assert_eq!(t.sent(), vec![answered(3, alt, TYPED_OUT)]);
}

#[test]
fn a_key_typed_to_be_decided_left_unanswered_is_passed_on_at_its_bound() {
    let (mut t, _k) = T::typing();
    t.decide_key(1, KEY_A, true);
    let a = t.typed_seq();
    t.pass(VERDICT_TIMEOUT - ms(1));
    assert!(t.w.emitted().is_empty());
    t.pass(ms(1));
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    assert_eq!(
        t.sent(),
        vec![ToClient::Expired { seq: a }, answered(1, a, TYPED_OUT)]
    );
    // A verdict that comes after changes nothing.
    t.verdict(a, true);
    assert!(t.sent().is_empty());
    t.decide_key(2, KEY_A, false);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1), (KEY_A, 0)]);
}

#[test]
fn a_client_that_answers_none_of_the_keys_it_types_is_dropped_as_one_that_answers_nothing() {
    let (mut t, k) = T::typing();
    let mut elapsed = Duration::ZERO;
    while !t.closed(CLIENT) {
        t.decide_key(1, KEY_B, true);
        t.pass(ms(50));
        t.decide_key(2, KEY_B, false);
        t.pass(ms(50));
        elapsed += ms(100);
        assert!(elapsed < Duration::from_secs(3), "never dropped");
    }
    assert!(elapsed >= UNANSWERED_SPAN, "dropped after {elapsed:?}");
    assert!(
        elapsed <= UNANSWERED_SPAN + ms(200),
        "dropped after {elapsed:?}"
    );
    assert_eq!(goodbye_reason(&t.sent()), Some(Goodbye::Unanswered));
    assert!(!t.w.is_open(k));
    t.assert_let_go();
}

#[test]
fn wait_holds_a_key_typed_to_be_decided() {
    let (mut t, _k) = T::typing_asking(FEATURE_TYPING | FEATURE_WAIT);
    t.decide_key(1, KEY_A, true);
    let a = t.typed_seq();
    t.wait(a);
    t.pass(VERDICT_WAIT_TIMEOUT - ms(10));
    assert!(t.w.emitted().is_empty());
    assert!(t.sent().is_empty());
    t.verdict(a, false);
    assert_eq!(t.w.emitted(), vec![(KEY_A, 1)]);
    assert_eq!(t.sent(), vec![answered(1, a, TYPED_OUT)]);
}

#[test]
fn the_watchdog_sees_a_key_typed_to_be_decided_waiting() {
    let (mut t, _k) = T::typing();
    let typed = t.now;
    t.decide_key(1, KEY_A, true);
    assert_eq!(t.core.oldest_undecided(), Some(typed));
    assert_eq!(t.core.next_wake(), Some(typed + VERDICT_TIMEOUT));
}

#[test]
fn keys_typed_to_be_decided_waiting_as_the_helper_lets_go_are_lost_and_never_go_out() {
    for way in ["stop", "chord", "console"] {
        let (mut t, k) = T::typing();
        t.decide_key(1, KEY_LEFTSHIFT, true);
        let shift = t.typed_seq();
        t.verdict(shift, false);
        assert_eq!(t.w.emitted(), vec![(KEY_LEFTSHIFT, 1)], "{way}");
        t.press(k, KEY_T);
        t.decide_key(2, KEY_A, true);
        let a = t.typed_seq();
        t.verdict(a, false);
        t.decide_key(3, KEY_A, false);
        let release = t.typed_seq();
        let reason = match way {
            "stop" => {
                t.send(FromClient::Stop);
                Released::Stopped
            }
            "chord" => {
                for c in CHORD.map(|keys| keys[0]) {
                    t.press(k, c);
                }
                Released::Emergency
            }
            _ => {
                t.console(3);
                Released::Background
            }
        };
        let sent = t.sent();
        let at = sent.iter().position(|m| *m == released(reason));
        let lost = [answered(2, a, TYPED_LOST), answered(3, release, TYPED_LOST)]
            .map(|m| sent.iter().position(|s| *s == m));
        assert!(at.is_some(), "{way}: {sent:?}");
        assert!(
            lost.iter().all(|i| i.is_some() && *i < at),
            "{way}: {sent:?}"
        );
        t.verdict(a, false);
        assert!(
            t.w.emitted().iter().all(|e| e.0 != KEY_A),
            "{way}: {:?}",
            t.w.emitted()
        );
        t.assert_let_go();
    }
}

#[test]
fn a_key_typed_to_be_decided_is_refused_as_it_goes_out_by_the_sysrq_rules() {
    let (mut t, k) = T::typing();
    t.press(k, KEY_LEFTALT);
    let alt = t.last_seq();
    t.decide_key(1, KEY_SYSRQ, true);
    let sysrq = t.typed_seq();
    t.verdict(sysrq, false);
    t.decide_key(2, KEY_SYSRQ, false);
    let release = t.typed_seq();
    assert!(t.sent().is_empty());
    t.verdict(alt, false);
    assert_eq!(
        t.sent(),
        vec![
            answered(1, sysrq, TYPED_REFUSED_KEY),
            answered(2, release, TYPED_NOT_DOWN)
        ]
    );
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1)]);
    no_sysrq_with_alt(&t.w.emitted());
}

#[test]
fn a_key_typed_to_be_decided_that_is_refused_at_once_is_sent_as_no_key() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.admit_asking(CLIENT, FEATURE_TYPING);
    t.decide_key(1, KEY_A, true);
    assert_eq!(t.sent(), vec![answered(1, 0, TYPED_REFUSED_STATE)]);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    t.sent();
    t.decide_key(2, KEY_POWER, true);
    t.decide_key(3, KEY_A, false);
    t.decide_key(4, KEY_A, true);
    t.decide_key(5, KEY_A, true);
    let sent = t.sent();
    assert_eq!(
        sent[..2],
        [
            answered(2, 0, TYPED_REFUSED_KEY),
            answered(3, 0, TYPED_NOT_DOWN)
        ],
        "{sent:?}"
    );
    // The client's KEYs are numbered from 1, refused keys spending no number.
    assert_eq!(
        sent[2..],
        [typed_key(1, KEY_A, true), answered(5, 0, TYPED_REFUSED_KEY)],
        "{sent:?}"
    );
    t.press(k, KEY_T);
    assert_eq!(t.last_seq(), 2);
}

#[test]
fn keys_typed_to_be_decided_count_neither_toward_the_emergency_chord_nor_against_it() {
    let (mut t, k) = T::typing();
    for (tag, c) in [(1, KEY_BACKSPACE), (2, KEY_ESC), (3, KEY_ENTER)] {
        t.decide_key(tag, c, true);
        let seq = t.typed_seq();
        t.verdict(seq, false);
    }
    assert_eq!(t.w.emitted().len(), 3);
    t.press(k, KEY_BACKSPACE);
    t.pass_last();
    t.press(k, KEY_ESC);
    t.pass_last();
    assert!(
        !t.sent().contains(&released(Released::Emergency)),
        "typed keys made the chord"
    );
    t.press(k, KEY_ENTER);
    assert!(t.sent().contains(&released(Released::Emergency)));
    t.assert_let_go();
}

/// The kernel never has the Alt's release, so its SysRq handler stays active for the virtual
/// keyboard; a key typed to be decided and passed behind it is not put out under it.
#[test]
fn no_key_typed_to_be_decided_goes_out_after_the_virtual_keyboard_failed_to_take_an_alt_release() {
    let (mut t, k) = T::typing();
    t.press(k, KEY_LEFTALT);
    t.pass_last();
    t.press(k, KEY_SYSRQ);
    let sysrq = t.last_seq();
    t.release(k, KEY_SYSRQ);
    t.release(k, KEY_LEFTALT);
    t.decide_key(1, KEY_B, true);
    let b = t.typed_seq();
    t.verdict(b, false);
    t.w.fail_emit_once(KEY_LEFTALT, 0);
    t.verdict(sysrq, false);
    assert!(t.sent().contains(&answered(1, b, TYPED_LOST)));
    assert!(t.w.emitted().iter().all(|e| e.0 != KEY_B));
}

// The late pass, with FEATURE_LATE_PASS: a kept modifier goes out just before a waiting press.

impl T {
    /// As capturing, with FEATURE_LATE_PASS, FEATURE_WAIT and FEATURE_TYPING granted.
    fn late_passing() -> (T, u32) {
        T::typing_asking(FEATURE_LATE_PASS | FEATURE_WAIT | FEATURE_TYPING)
    }

    fn late_pass(&mut self, kept: u64, waiting: u64) {
        self.send(FromClient::LatePass { kept, waiting });
    }

    /// Reads a press, sends nothing for it, and answers its number.
    fn pressed(&mut self, k: u32, code: u16) -> u64 {
        self.press(k, code);
        self.last_seq()
    }

    /// Reads a press and keeps it, and answers its number.
    fn kept(&mut self, k: u32, code: u16) -> u64 {
        let seq = self.pressed(k, code);
        self.verdict(seq, true);
        seq
    }

    fn malformed(&mut self) -> String {
        let sent = self.sent();
        let why = sent.iter().find_map(|m| match m {
            ToClient::Goodbye {
                reason: Goodbye::Malformed,
                text,
                ..
            } => Some(text.clone()),
            _ => None,
        });
        assert!(self.closed(CLIENT), "{sent:?}");
        why.unwrap_or_else(|| panic!("no GOODBYE MALFORMED: {sent:?}"))
    }
}

/// Alt held under the screen reader's key and kept, then Tab, which the client passes: Alt goes on
/// the virtual keyboard just before Tab, and comes up when the hand lets it go. No message
/// answers the LATE_PASS.
#[test]
fn a_kept_alt_late_passed_goes_out_just_before_the_key_and_up_with_the_hand() {
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    assert!(t.w.emitted().is_empty(), "{:?}", t.w.emitted());
    t.late_pass(alt, tab);
    assert_eq!(t.sent(), vec![], "the LATE_PASS was answered");
    assert!(t.w.emitted().is_empty(), "went out before the verdict");
    t.verdict(tab, false);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1), (KEY_TAB, 1)]);
    t.release(k, KEY_TAB);
    assert_eq!(t.w.emitted()[2..], [(KEY_TAB, 0)]);
    assert_eq!(t.w.virtual_down(), {
        let mut s = KeySet::new();
        s.insert(KEY_LEFTALT);
        s
    });
    // Tab again, under the same Alt, now down on the virtual keyboard: no LATE_PASS needed.
    let again = t.pressed(k, KEY_TAB);
    t.verdict(again, false);
    t.release(k, KEY_TAB);
    t.release(k, KEY_LEFTALT);
    assert_eq!(
        t.w.emitted()[3..],
        [(KEY_TAB, 1), (KEY_TAB, 0), (KEY_LEFTALT, 0)]
    );
    assert!(t.w.virtual_down().is_empty());
    assert!(!t.closed(CLIENT));
    t.send(FromClient::Stop);
    t.assert_let_go();
}

/// Alt let go of after Tab was read, before the LATE_PASS came: it still goes out before Tab, and
/// its release behind Tab's, since a modifier's release keeps its place in line.
#[test]
fn a_kept_key_let_go_of_after_the_waiting_press_was_read_still_goes_out_before_it() {
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.send(FromClient::Wait { seq: tab });
    t.release(k, KEY_TAB);
    t.release(k, KEY_LEFTALT);
    assert!(t.w.emitted().is_empty());
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(
        t.w.emitted(),
        vec![
            (KEY_LEFTALT, 1),
            (KEY_TAB, 1),
            (KEY_TAB, 0),
            (KEY_LEFTALT, 0)
        ]
    );
    assert!(t.w.virtual_down().is_empty());
}

/// The kept key goes out in the press's place whatever becomes of the press: kept too, Alt alone
/// goes out, and up with the hand.
#[test]
fn a_late_pass_goes_out_though_the_waiting_press_is_kept_or_passed_at_its_bound() {
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, true);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1)]);
    t.release(k, KEY_TAB);
    t.release(k, KEY_LEFTALT);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1), (KEY_LEFTALT, 0)]);

    let ctrl = t.kept(k, KEY_LEFTCTRL);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(ctrl, tab);
    t.pass(VERDICT_TIMEOUT);
    assert!(t.sent().contains(&ToClient::Expired { seq: tab }));
    assert_eq!(t.w.emitted()[2..], [(KEY_LEFTCTRL, 1), (KEY_TAB, 1)]);
    t.release(k, KEY_TAB);
    t.release(k, KEY_LEFTCTRL);
    assert!(t.w.virtual_down().is_empty());
}

/// Control and Alt held under the screen reader's key, then Tab: each named goes out just before
/// Tab, in the order the LATE_PASSes came.
#[test]
fn several_kept_keys_late_passed_before_one_press_go_out_in_the_order_named() {
    let (mut t, k) = T::late_passing();
    let ctrl = t.kept(k, KEY_LEFTCTRL);
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.late_pass(ctrl, tab);
    t.verdict(tab, false);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_LEFTALT, 1), (KEY_LEFTCTRL, 1), (KEY_TAB, 1)]
    );
    t.release(k, KEY_TAB);
    t.release(k, KEY_LEFTALT);
    t.release(k, KEY_LEFTCTRL);
    assert!(t.w.virtual_down().is_empty());
    assert!(!t.closed(CLIENT));
}

/// What would keep a LATE_PASS from applying can change on its way, so it changes nothing and the
/// connection is kept: the first was passed, or expired before a keep came; it went down after the
/// second was read, or came up before; the second is answered already.
#[test]
fn a_late_pass_that_cannot_apply_changes_nothing() {
    // Passed: Alt is out already, and goes out once.
    let (mut t, k) = T::late_passing();
    let alt = t.pressed(k, KEY_LEFTALT);
    t.verdict(alt, false);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1), (KEY_TAB, 1)]);
    t.release(k, KEY_TAB);
    t.release(k, KEY_LEFTALT);
    assert!(t.w.virtual_down().is_empty());

    // Expired, a keep coming after it: passed as well.
    let (mut t, k) = T::late_passing();
    let alt = t.pressed(k, KEY_LEFTALT);
    t.pass(VERDICT_TIMEOUT);
    t.verdict(alt, true);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1), (KEY_TAB, 1)]);

    // Let go of before the second was read.
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    t.release(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1)]);

    // Let go of before the second was read, and pressed and kept again: the press named is not
    // the one down.
    let (mut t, k) = T::late_passing();
    let first = t.kept(k, KEY_LEFTALT);
    t.release(k, KEY_LEFTALT);
    t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(first, tab);
    t.verdict(tab, false);
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1)]);
    assert!(!t.closed(CLIENT));

    // Pressed after the second was read, and kept while the second waited.
    let (mut t, k) = T::late_passing();
    let tab = t.pressed(k, KEY_TAB);
    t.send(FromClient::Wait { seq: tab });
    let alt = t.kept(k, KEY_LEFTALT);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1)]);

    // The second answered already, though still in line behind a press that waits.
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    let shift = t.pressed(k, KEY_LEFTSHIFT);
    t.send(FromClient::Wait { seq: shift });
    let tab = t.pressed(k, KEY_TAB);
    t.verdict(tab, false);
    t.late_pass(alt, tab);
    t.verdict(shift, false);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTSHIFT, 1), (KEY_TAB, 1)]);
    for (k, code) in [(k, KEY_TAB), (k, KEY_LEFTSHIFT), (k, KEY_LEFTALT)] {
        t.release(k, code);
    }
    assert!(t.w.virtual_down().is_empty());
    assert!(!t.closed(CLIENT));
}

/// A kept Alt still in line, its keep counted behind a press that waits, counts as kept.
#[test]
fn a_keep_counted_while_the_kept_press_is_still_in_line_counts() {
    let (mut t, k) = T::late_passing();
    let shift = t.pressed(k, KEY_LEFTSHIFT);
    t.send(FromClient::Wait { seq: shift });
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert!(t.w.emitted().is_empty());
    t.verdict(shift, false);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_LEFTSHIFT, 1), (KEY_LEFTALT, 1), (KEY_TAB, 1)]
    );
}

/// Keys read for a case, answering the kept press and the waiting press a LATE_PASS then names.
type LatePassCase = fn(&mut T, u32) -> (u64, u64);

/// Only what the client knew as it sent a LATE_PASS makes it malformed, which ends the connection
/// and lets go; the words name no key.
#[test]
fn a_late_pass_naming_keys_it_may_not_is_malformed() {
    let cases: [(&str, LatePassCase); 10] = [
        ("never sent", |t, k| (0, t.pressed(k, KEY_TAB))),
        ("never sent", |t, k| {
            let alt = t.kept(k, KEY_LEFTALT);
            (alt, alt + 5)
        }),
        ("not a press of a modifier", |t, k| {
            let a = t.kept(k, KEY_A);
            (a, t.pressed(k, KEY_TAB))
        }),
        ("not a press of a modifier", |t, k| {
            let caps = t.kept(k, KEY_CAPSLOCK);
            (caps, t.pressed(k, KEY_TAB))
        }),
        ("not a press of a modifier", |t, k| {
            let alt = t.kept(k, KEY_LEFTALT);
            t.release(k, KEY_LEFTALT);
            (alt + 1, t.pressed(k, KEY_TAB))
        }),
        ("not a press of a modifier", |t, k| {
            t.decide_key(1, KEY_LEFTALT, true);
            let alt = t.typed_seq();
            t.verdict(alt, true);
            (alt, t.pressed(k, KEY_TAB))
        }),
        ("not a press of a modifier", |t, k| {
            let alt = t.kept(k, KEY_LEFTALT);
            t.w.repeat(k, KEY_LEFTALT);
            t.core.keyboard_ready(k, t.now);
            t.take();
            let repeat = t.last_seq();
            assert_eq!(repeat, alt + 1);
            (repeat, t.pressed(k, KEY_TAB))
        }),
        ("named before", |t, k| {
            let alt = t.kept(k, KEY_LEFTALT);
            let tab = t.pressed(k, KEY_TAB);
            t.late_pass(alt, tab);
            t.verdict(tab, false);
            t.release(k, KEY_TAB);
            (alt, t.pressed(k, KEY_TAB))
        }),
        ("not a press asking for a verdict", |t, k| {
            let alt = t.kept(k, KEY_LEFTALT);
            t.pressed(k, KEY_TAB);
            t.release(k, KEY_TAB);
            (alt, t.last_seq())
        }),
        ("not a press asking for a verdict", |t, k| {
            let alt = t.kept(k, KEY_LEFTALT);
            let tab = t.pressed(k, KEY_TAB);
            t.verdict(tab, false);
            t.w.repeat(k, KEY_TAB);
            t.core.keyboard_ready(k, t.now);
            t.take();
            (alt, t.last_seq())
        }),
    ];
    for (n, (why, make)) in cases.into_iter().enumerate() {
        let (mut t, k) = T::late_passing();
        let (kept, waiting) = make(&mut t, k);
        t.sent();
        t.late_pass(kept, waiting);
        let said = t.malformed();
        assert!(said.contains(why), "case {n}: {said}");
        assert!(said.starts_with("a LATE_PASS"), "case {n}: {said}");
        assert!(
            !said.chars().any(|c| c.is_ascii_digit()),
            "case {n}: {said}"
        );
        t.assert_let_go();
    }
}

/// From a client not granted the feature, as Linda's X route is, a LATE_PASS is of a type it does
/// not know, and ends the connection.
#[test]
fn a_late_pass_from_a_client_not_granted_the_feature_ends_the_connection() {
    let (mut t, k) = T::typing_asking(FEATURE_WAIT);
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    assert!(t.malformed().contains("unknown message type 0x07"));
    assert!(t.w.emitted().iter().all(|e| e.0 != KEY_LEFTALT));
    t.assert_let_go();
}

/// Letting go before the waiting press's turn: the kept key never goes out, and a press kept
/// before a let-go never counts as kept for a LATE_PASS after it.
#[test]
fn a_late_pass_does_not_outlive_a_let_go() {
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.send(FromClient::Wait { seq: tab });
    t.late_pass(alt, tab);
    t.send(FromClient::Stop);
    // Tab, waiting, is passed on as the helper lets go; Alt is not.
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1), (KEY_TAB, 0)]);
    t.assert_let_go();

    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    // The session leaves the front with Alt still held, and comes back once it is up. Alt is
    // pressed and kept afresh, so what is named is all that keeps it from going out.
    t.logind(front("c1", 1000));
    t.w.release(k, KEY_LEFTALT);
    t.logind(front(SESSION, 1000));
    assert!(t.w.grabbed(k));
    t.sent();
    t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1)]);
    assert!(!t.closed(CLIENT));
}

/// A client that connects again, as Linda does after a loss, numbers its keys from one again, and
/// is judged by the keys sent to it and not by those its first connection was sent.
#[test]
fn a_client_admitted_again_is_judged_by_its_own_keys() {
    let (mut t, k) = T::late_passing();
    t.kept(k, KEY_A);
    t.release(k, KEY_A);
    t.core.disconnected(CLIENT, t.now);
    t.take();
    t.out.clear();
    t.admit_asking(CLIENT, FEATURE_LATE_PASS | FEATURE_WAIT);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    t.sent();
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert!(!t.closed(CLIENT), "{:?}", t.sent());
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1), (KEY_TAB, 1)]);
}

/// A key typed to be decided is a press asking for a verdict like any other, and a kept Alt goes
/// out before it; a typed SysRq after it is refused, Alt being down.
#[test]
fn a_late_pass_goes_out_before_a_key_typed_to_be_decided() {
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    t.decide_key(1, KEY_TAB, true);
    let tab = t.typed_seq();
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(t.sent(), vec![answered(1, tab, TYPED_OUT)]);
    assert_eq!(t.w.emitted(), vec![(KEY_LEFTALT, 1), (KEY_TAB, 1)]);
    t.type_key(2, KEY_SYSRQ, true);
    assert_eq!(t.typed(), vec![(2, TYPED_REFUSED_KEY)]);
    t.decide_key(3, KEY_TAB, false);
    t.release(k, KEY_LEFTALT);
    assert!(t.w.virtual_down().is_empty(), "{:?}", t.w.emitted());
}

/// The kept key's keyboard vanishes while the press waits: its made-up release is behind the
/// press, so the kept key still goes out before it and comes up after.
#[test]
fn a_late_passed_key_whose_keyboard_vanished_is_released() {
    let (mut t, k) = T::late_passing();
    let other = t.w.plug("USB keyboard");
    t.core.watch_ready(t.now);
    t.take();
    assert!(t.w.grabbed(other));
    t.sent();
    let alt = t.kept(k, KEY_LEFTALT);
    let tab = t.pressed(other, KEY_TAB);
    t.send(FromClient::Wait { seq: tab });
    t.w.unplug(k);
    t.core.keyboard_ready(k, t.now);
    t.take();
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert_eq!(
        t.w.emitted(),
        vec![(KEY_LEFTALT, 1), (KEY_TAB, 1), (KEY_LEFTALT, 0)]
    );
    t.release(other, KEY_TAB);
    assert!(t.w.virtual_down().is_empty());
}

/// A LATE_PASS naming a kept key older than LATE_PASS_RECALL KEYs changes nothing, and is not
/// taken for malformed, since the helper can no longer tell.
#[test]
fn a_late_pass_naming_a_key_forgotten_changes_nothing() {
    let (mut t, k) = T::late_passing();
    let alt = t.kept(k, KEY_LEFTALT);
    for _ in 0..LATE_PASS_RECALL / 2 {
        let a = t.pressed(k, KEY_A);
        t.verdict(a, true);
        t.release(k, KEY_A);
    }
    let tab = t.pressed(k, KEY_TAB);
    t.late_pass(alt, tab);
    t.verdict(tab, false);
    assert!(!t.closed(CLIENT), "{:?}", t.sent());
    assert_eq!(t.w.emitted(), vec![(KEY_TAB, 1)]);
    // Kept to its bound however long the client is served.
    assert_eq!(t.core.told.len(), LATE_PASS_RECALL);
}

/// Nothing of it is remembered for a client not granted the feature, as Linda's X route is not.
#[test]
fn nothing_is_remembered_for_a_client_not_granted_the_late_pass() {
    let (mut t, k) = T::typing_asking(FEATURE_WAIT);
    let alt = t.kept(k, KEY_LEFTALT);
    t.release(k, KEY_LEFTALT);
    assert!(alt > 0);
    assert!(t.core.told.is_empty() && t.core.late.is_empty());
}

// The light probe.

const EVERY_LIGHT: u16 = 0b1_1111;

impl T {
    /// A keyboard with `leds` lit, and a client granted the light probe, which asks to capture: the
    /// virtual keyboard is made and lit, and the kernel has handed back the echo.
    fn probing(leds: u16) -> (T, u32) {
        let mut t = T::new();
        let k = t.w.plug("keyboard");
        t.w.set_device_leds(k, leds);
        let asked = FEATURE_WAIT | FEATURE_TYPING | FEATURE_LATE_PASS | FEATURE_VIRTUAL_SEEN;
        assert_eq!(t.admit_asking(CLIENT, asked), asked);
        t.send(FromClient::Start);
        t.core.virtual_ready(t.now);
        t.take();
        (t, k)
    }

    /// What the desktop does once it has the virtual keyboard: writes every light.
    fn desktop_writes(&mut self, leds: u16) {
        self.pass(ms(60));
        self.desktop_leds(leds);
    }

    /// The VIRTUAL sent, and whether it came before the first DEVICE.
    fn virtual_word(sent: &[ToClient]) -> (Vec<u8>, bool) {
        let words: Vec<u8> = sent
            .iter()
            .filter_map(|m| match m {
                ToClient::Virtual { status } => Some(*status),
                _ => None,
            })
            .collect();
        let at = |f: &dyn Fn(&ToClient) -> bool| sent.iter().position(f);
        let before = match (
            at(&|m| matches!(m, ToClient::Virtual { .. })),
            at(&|m| matches!(m, ToClient::Device { .. })),
        ) {
            (Some(v), Some(d)) => v < d,
            (Some(_), None) => true,
            _ => false,
        };
        (words, before)
    }

    fn locks_told(sent: &[ToClient]) -> Vec<u16> {
        sent.iter()
            .filter_map(|m| match m {
                ToClient::Locks { leds } => Some(*leds),
                _ => None,
            })
            .collect()
    }
}

/// GNOME with every lock off: mutter writes every light off some 0.06 s after the keyboard is made,
/// which changes each one the probe lit. SEEN comes before the keyboard is grabbed, nothing of the
/// probe's lights reaches the client or a keyboard, and the locks are the keyboard's, as without it.
#[test]
fn a_desktop_that_writes_the_lights_is_seen() {
    let (mut t, k) = T::probing(0);
    assert_eq!(t.w.own_leds(), vec![EVERY_LIGHT]);
    assert_eq!(t.w.virtual_leds(), EVERY_LIGHT);
    assert!(t.sent().is_empty(), "the echo of the probe was told");
    t.desktop_writes(0);
    assert!(
        t.sent().is_empty(),
        "told before the virtual keyboard was ready"
    );
    assert!(!t.w.is_open(k));
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(t.w.grabbed(k), "{sent:?}");
    assert_eq!(
        T::virtual_word(&sent),
        (vec![VIRTUAL_SEEN], true),
        "{sent:?}"
    );
    assert_eq!(T::locks_told(&sent), vec![0]);
    assert_eq!((t.w.virtual_leds(), t.w.leds(k)), (0, 0));
    assert_eq!(
        t.w.own_leds(),
        vec![EVERY_LIGHT],
        "a desktop's lights were put back"
    );
    // A later change of the locks is the desktop's word, as ever.
    t.desktop_leds(LOCK_CAPS);
    assert_eq!(t.sent(), vec![ToClient::Locks { leds: LOCK_CAPS }]);
    assert_eq!(t.w.leds(k), LOCK_CAPS);
}

/// GNOME, and KWin or X, with Num Lock on: the desktop writes Num Lock on, which the probe had lit
/// already, and the rest off. Seen, and Num Lock is the desktop's word on the locks, as it would have
/// been with no probe; the keyboard shows it and no other light.
#[test]
fn a_desktop_with_num_lock_on_is_seen_and_its_lock_told() {
    let (mut t, k) = T::probing(0);
    t.desktop_writes(LOCK_NUM);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert_eq!(T::virtual_word(&sent).0, vec![VIRTUAL_SEEN]);
    let locks = T::locks_told(&sent);
    assert!(
        !locks.is_empty() && locks.iter().all(|l| *l == LOCK_NUM),
        "{sent:?}"
    );
    assert_eq!((t.w.virtual_leds(), t.w.leds(k)), (LOCK_NUM, LOCK_NUM));
}

/// sway writes every light off whatever the locks: seen, and the locks then come from the first
/// keyboard grabbed, as they did before the probe.
#[test]
fn sways_lights_off_are_seen_and_the_locks_come_as_before() {
    let (mut t, k) = T::probing(LOCK_NUM);
    t.desktop_writes(0);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert_eq!(T::virtual_word(&sent).0, vec![VIRTUAL_SEEN]);
    assert_eq!(T::locks_told(&sent), vec![LOCK_NUM]);
    assert_eq!(t.w.leds(k), LOCK_NUM);
}

/// KWin and X with every lock off write nothing: unseen, the lights are put back to none before
/// anything is grabbed, and the locks come from the first keyboard grabbed, as before.
#[test]
fn a_desktop_that_writes_nothing_is_unseen_and_the_lights_are_put_back() {
    let (mut t, k) = T::probing(0);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(t.w.grabbed(k));
    assert_eq!(
        T::virtual_word(&sent),
        (vec![VIRTUAL_UNSEEN], true),
        "{sent:?}"
    );
    assert_eq!(t.w.own_leds(), vec![EVERY_LIGHT, 0]);
    assert_eq!(t.w.virtual_leds(), 0);
    // The echo of putting them back is nobody's word either.
    t.core.virtual_ready(t.now);
    t.take();
    assert_eq!(T::locks_told(&sent), vec![0]);
    assert!(t.sent().is_empty());
    assert_eq!(t.w.leds(k), 0);
}

/// VIRTUAL comes once for each client. A client after it finds the virtual keyboard made already:
/// OLD, and nothing is lit. The same client asking again after STOP is told nothing more.
#[test]
fn a_virtual_keyboard_made_before_the_client_is_old() {
    let (mut t, k) = T::probing(0);
    t.desktop_writes(0);
    t.pass(VIRTUAL_SETTLE);
    assert_eq!(T::virtual_word(&t.sent()).0, vec![VIRTUAL_SEEN]);
    t.send(FromClient::Stop);
    t.send(FromClient::Start);
    let sent = t.sent();
    assert!(t.w.grabbed(k));
    assert!(T::virtual_word(&sent).0.is_empty(), "{sent:?}");
    t.core.disconnected(CLIENT, t.now);
    t.take();
    let asked = FEATURE_WAIT | FEATURE_VIRTUAL_SEEN;
    assert_eq!(t.admit_asking(OTHER, asked), asked);
    t.from(OTHER, FromClient::Start);
    let sent = t.sent_to(OTHER);
    assert!(t.w.grabbed(k));
    assert_eq!(
        T::virtual_word(&sent),
        (vec![VIRTUAL_OLD], true),
        "{sent:?}"
    );
    assert_eq!(t.w.own_leds(), vec![EVERY_LIGHT]);
    assert_eq!(t.w.virtual_made(), 1);
}

/// A client that left while the virtual keyboard settled: the lights are still put back, and the
/// next client is told OLD, as the keyboard was made before its START.
#[test]
fn a_probe_whose_client_left_is_put_back_and_its_successor_told_old() {
    let (mut t, k) = T::probing(0);
    t.core.disconnected(CLIENT, t.now);
    t.take();
    let asked = FEATURE_WAIT | FEATURE_VIRTUAL_SEEN;
    assert_eq!(t.admit_asking(OTHER, asked), asked);
    t.from(OTHER, FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent_to(OTHER);
    assert!(t.w.grabbed(k));
    assert_eq!(
        T::virtual_word(&sent),
        (vec![VIRTUAL_OLD], true),
        "{sent:?}"
    );
    assert_eq!(t.w.own_leds(), vec![EVERY_LIGHT, 0]);
    assert_eq!(t.w.virtual_leds(), 0);
}

/// The session left the front while the virtual keyboard settled, and what wrote the lights may
/// have been the console or another session: OLD once the client captures again, and the lights,
/// with none of the client's session's, are not told it.
#[test]
fn a_probe_while_the_session_was_away_proves_nothing() {
    let (mut t, k) = T::probing(0);
    t.console(X_CONSOLE);
    t.console(2);
    t.desktop_writes(LOCK_NUM);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(!t.w.is_open(k));
    assert!(T::virtual_word(&sent).0.is_empty(), "{sent:?}");
    assert!(T::locks_told(&sent).is_empty(), "{sent:?}");
    t.console(X_CONSOLE);
    t.logind(front(SESSION, 1000));
    let sent = t.sent();
    assert!(t.w.grabbed(k));
    assert_eq!(
        T::virtual_word(&sent),
        (vec![VIRTUAL_OLD], true),
        "{sent:?}"
    );
    assert!(!T::locks_told(&sent).contains(&LOCK_NUM), "{sent:?}");
}

/// The session left and came back while the virtual keyboard settled, and is in front as it ends:
/// what wrote the lights meanwhile may still have been the console or another session, so OLD.
#[test]
fn a_probe_while_the_session_was_away_and_back_proves_nothing() {
    let (mut t, k) = T::probing(0);
    t.console(X_CONSOLE);
    t.console(2);
    t.desktop_writes(LOCK_NUM);
    t.console(X_CONSOLE);
    t.logind(front(SESSION, 1000));
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(t.w.grabbed(k));
    assert_eq!(
        T::virtual_word(&sent),
        (vec![VIRTUAL_OLD], true),
        "{sent:?}"
    );
}

/// The kernel hands the console's locks to a keyboard as it is made, inside UI_DEV_CREATE and so
/// before the probe lights it: here Num Lock, as setleds at boot leaves the graphical console. KWin
/// with every lock off then writes nothing. That is no desktop's write: UNSEEN, and none of the
/// probe's lights is told or shown.
#[test]
fn the_consoles_lights_at_creation_are_no_desktops_write() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.desktop_writes_leds(&[(LED_NUML, true)]);
    let asked = FEATURE_WAIT | FEATURE_VIRTUAL_SEEN;
    assert_eq!(t.admit_asking(CLIENT, asked), asked);
    t.send(FromClient::Start);
    t.core.virtual_ready(t.now);
    t.take();
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(t.w.grabbed(k));
    assert_eq!(
        T::virtual_word(&sent),
        (vec![VIRTUAL_UNSEEN], true),
        "{sent:?}"
    );
    assert_eq!(T::locks_told(&sent), vec![0], "{sent:?}");
    assert_eq!((t.w.virtual_leds(), t.w.leds(k)), (0, 0));
}

/// A keyboard added while the virtual keyboard settles makes the kernel hand the console's locks,
/// here none, to every keyboard: Num, Caps, Scroll and Kana Lock go off on the virtual keyboard, and
/// Compose, which no trigger drives, stays on. KWin with every lock off writes nothing: UNSEEN, and
/// the Compose light is neither told nor shown.
#[test]
fn the_consoles_lights_on_a_keyboard_added_are_no_desktops_write() {
    let (mut t, k) = T::probing(0);
    t.pass(ms(300));
    t.w.desktop_writes_leds(&[
        (LED_NUML, false),
        (LED_CAPSL, false),
        (LED_SCROLLL, false),
        (LED_KANA, false),
    ]);
    t.core.virtual_ready(t.now);
    t.take();
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(t.w.grabbed(k));
    assert_eq!(T::virtual_word(&sent).0, vec![VIRTUAL_UNSEEN], "{sent:?}");
    assert_eq!(T::locks_told(&sent), vec![0], "{sent:?}");
    assert_eq!((t.w.virtual_leds(), t.w.leds(k)), (0, 0));
}

/// A virtual keyboard that cannot be lit for the probe is one that does not take writes: it fails
/// as one that cannot be made does, and nothing is grabbed.
#[test]
fn a_virtual_keyboard_that_cannot_be_lit_fails() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    t.w.fail_emit(true);
    let asked = FEATURE_WAIT | FEATURE_VIRTUAL_SEEN;
    assert_eq!(t.admit_asking(CLIENT, asked), asked);
    t.send(FromClient::Start);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(
        sent.contains(&released(Released::VirtualKeyboard)),
        "{sent:?}"
    );
    assert!(T::virtual_word(&sent).0.is_empty());
    assert!(!t.w.virtual_exists() && !t.w.is_open(k));
}

/// Linda's X route asks for FEATURE_WAIT alone: the virtual keyboard is never lit, no VIRTUAL comes,
/// and the desktop's lights are its word on the locks the moment it writes them, as before.
#[test]
fn a_client_not_asking_for_the_probe_has_the_lights_as_before() {
    let mut t = T::new();
    let k = t.w.plug("keyboard");
    assert_eq!(t.admit_asking(CLIENT, FEATURE_WAIT), FEATURE_WAIT);
    t.send(FromClient::Start);
    t.core.virtual_ready(t.now);
    t.take();
    assert!(t.w.own_leds().is_empty());
    assert_eq!(t.w.virtual_leds(), 0);
    t.desktop_writes(LOCK_NUM);
    assert_eq!(t.sent(), vec![ToClient::Locks { leds: LOCK_NUM }]);
    t.pass(VIRTUAL_SETTLE);
    let sent = t.sent();
    assert!(t.w.grabbed(k));
    assert!(T::virtual_word(&sent).0.is_empty(), "{sent:?}");
    assert_eq!(t.w.leds(k), LOCK_NUM);
}
