//! The whole helper but the devices and the system bus: its real loop, socket, admission thread and
//! watchdog thread, with the mocked devices and a stand-in for logind and polkit, on the real
//! clock. The bounds checked are the protocol's own, with a margin for a loaded machine.

use std::path::PathBuf;
use std::sync::atomic::AtomicUsize;
use std::thread::JoinHandle;

use super::*;
use crate::client::{Client, Got};
use crate::keys::{KEY_A, KEY_LEFTSHIFT};
use crate::mock::World;
use crate::protocol::*;
use crate::watchdog;

/// How much later than its bound a thing may happen here and still pass.
const MARGIN: Duration = Duration::from_millis(900);
const SESSION: &str = "2";

struct Helper {
    w: World,
    dir: PathBuf,
    socket: PathBuf,
    notices: Arc<Notices>,
    stop: Arc<AtomicBool>,
    ended: Arc<AtomicBool>,
    thread: Option<JoinHandle<io::Result<()>>>,
}

static NEXT: AtomicUsize = AtomicUsize::new(0);

impl Helper {
    /// The helper, with every caller admitted to the session in front, which is this user's.
    fn start() -> Helper {
        Helper::start_with(|_| Ok(SESSION.into()))
    }

    fn start_with(decide: impl FnMut(&Peer) -> Result<String, Refusal> + Send + 'static) -> Helper {
        let dir = std::env::temp_dir().join(format!(
            "keyd-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let socket = dir.join("socket");
        let w = World::new();
        let listener = bind(&socket).unwrap();
        let notices = Notices::new().unwrap();
        let admit = spawn_admitter(notices.clone(), decide).unwrap();
        let core = Core::new(w.devices());
        let progress = Progress::new();
        let ended = Arc::new(AtomicBool::new(false));
        let e = ended.clone();
        watchdog::spawn(
            progress.clone(),
            core.devices().guard(),
            Box::new(move || e.store(true, Relaxed)),
        )
        .unwrap();
        let mut server = Server::new(core, listener, notices.clone(), admit, progress, None);
        let stop = server.stopper();
        // SAFETY: getuid cannot fail.
        let uid = unsafe { libc::getuid() };
        notices.post(Notice::Front(Front::Session {
            id: SESSION.into(),
            uid,
            vt: 7,
        }));
        let thread = Some(thread::spawn(move || server.run()));
        Helper {
            w,
            dir,
            socket,
            notices,
            stop,
            ended,
            thread,
        }
    }

    fn client(&self) -> Client {
        Client::connect(&self.socket).unwrap()
    }

    /// A client admitted, capturing, and holding the keyboard given.
    fn capturing(&self, k: u32) -> Client {
        let c = self.client();
        assert!(matches!(c.hello(), ToClient::Welcome { .. }));
        c.send(&FromClient::Start);
        c.until(VIRTUAL_SETTLE + MARGIN, |m| {
            matches!(m, ToClient::Device { device, status: DeviceStatus::Held, .. } if *device == k)
        });
        assert!(self.w.grabbed(k));
        c
    }
}

impl Drop for Helper {
    fn drop(&mut self) {
        self.stop.store(true, Relaxed);
        self.notices.post(Notice::Wake);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
        let _ = fs::remove_dir_all(&self.dir);
    }
}

fn key(m: &ToClient) -> Option<Key> {
    match m {
        ToClient::Key(k) => Some(*k),
        _ => None,
    }
}

/// Waits for a condition the helper's thread brings about.
fn eventually(within: Duration, what: &str, f: impl Fn() -> bool) {
    let end = Instant::now() + within;
    while !f() {
        assert!(Instant::now() < end, "not within {within:?}: {what}");
        thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn a_key_goes_round_through_the_socket_in_well_under_its_bound() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    let mut took = Vec::new();
    for _ in 0..20 {
        let pressed = Instant::now();
        h.w.press(k, KEY_A);
        let got = c.until(MARGIN, |m| key(m).is_some());
        let k1 = key(got.last().unwrap()).unwrap();
        assert_eq!((k1.code, k1.value, k1.flags), (KEY_A, KEY_PRESS, KEY_ASK));
        c.send(&FromClient::Verdict {
            seq: k1.seq,
            keep: false,
        });
        eventually(MARGIN, "the key out", || h.w.virtual_down().contains(KEY_A));
        took.push(pressed.elapsed());
        h.w.release(k, KEY_A);
        c.until(MARGIN, |m| key(m).is_some_and(|k| k.value == KEY_RELEASE));
        eventually(MARGIN, "the release out", || h.w.virtual_down().is_empty());
    }
    // A turn of the loop each way and the test's own polling; a round that a loaded machine held
    // up past the bound would have been passed on all the same, so only the usual case is held to
    // it.
    took.sort();
    assert!(took[took.len() / 2] < VERDICT_TIMEOUT / 4, "{took:?}");
}

#[test]
fn a_client_that_goes_silent_is_let_go_of_in_time() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    h.w.press(k, KEY_LEFTSHIFT);
    let got = c.until(MARGIN, |m| key(m).is_some());
    c.send(&FromClient::Verdict {
        seq: key(got.last().unwrap()).unwrap().seq,
        keep: false,
    });
    eventually(MARGIN, "Shift down", || {
        h.w.virtual_down().contains(KEY_LEFTSHIFT)
    });
    let quiet = Instant::now();
    let got = c.until_closed(HEARTBEAT_TIMEOUT + MARGIN);
    let after = quiet.elapsed();
    assert!(
        after >= HEARTBEAT_TIMEOUT - Duration::from_millis(50),
        "{after:?}"
    );
    assert!(got.iter().any(|m| matches!(
        m,
        ToClient::Goodbye {
            reason: Goodbye::Silent,
            ..
        }
    )));
    assert!(!h.w.any_grabbed());
    assert!(h.w.virtual_down().is_empty());
    assert!(!h.w.is_open(k));
}

#[test]
fn a_client_that_answers_nothing_has_its_keys_passed_on_after_the_bound() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    let pressed = Instant::now();
    h.w.press(k, KEY_A);
    eventually(VERDICT_TIMEOUT + MARGIN, "the key out", || {
        h.w.virtual_down().contains(KEY_A)
    });
    assert!(pressed.elapsed() >= VERDICT_TIMEOUT);
    let got = c.until(MARGIN, |m| matches!(m, ToClient::Expired { .. }));
    let seq = got.iter().find_map(key).unwrap().seq;
    assert_eq!(got.last(), Some(&ToClient::Expired { seq }));
}

#[test]
fn a_client_that_dies_lets_everything_go() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    h.w.press(k, KEY_LEFTSHIFT);
    let got = c.until(MARGIN, |m| key(m).is_some());
    c.send(&FromClient::Verdict {
        seq: key(got.last().unwrap()).unwrap().seq,
        keep: false,
    });
    eventually(MARGIN, "Shift down", || {
        h.w.virtual_down().contains(KEY_LEFTSHIFT)
    });
    drop(c);
    eventually(MARGIN, "let go", || {
        !h.w.any_grabbed() && h.w.virtual_down().is_empty() && !h.w.is_open(k)
    });
}

#[test]
fn rubbish_on_the_socket_ends_the_connection() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    c.send_raw(&[0x42, 1, 2, 3]);
    let got = c.until_closed(MARGIN);
    assert!(got.iter().any(|m| matches!(
        m,
        ToClient::Goodbye {
            reason: Goodbye::Malformed,
            ..
        }
    )));
    eventually(MARGIN, "let go", || !h.w.any_grabbed());
    // So does a message too long to be one.
    let c = h.client();
    c.send_raw(&[HEARTBEAT; MAX_MESSAGE * 2]);
    let got = c.until_closed(MARGIN);
    assert!(matches!(
        got[0],
        ToClient::Goodbye {
            reason: Goodbye::Malformed,
            ..
        }
    ));
}

#[test]
fn goodbye_reaches_a_client_whose_last_messages_were_not_read() {
    // The kernel answers a read with ECONNRESET, before the GOODBYE still queued for it, when the
    // other end closed with messages of the reader's unread.
    for _ in 0..20 {
        let h = Helper::start();
        let k = h.w.plug("keyboard");
        let c = h.capturing(k);
        c.send_raw(&[0x42, 1, 2, 3]);
        for counter in 0..8 {
            c.try_send(&FromClient::Heartbeat { counter });
        }
        let got = c.until_closed(MARGIN);
        assert!(
            got.iter().any(|m| matches!(
                m,
                ToClient::Goodbye {
                    reason: Goodbye::Malformed,
                    ..
                }
            )),
            "{got:?}"
        );
    }
}

#[test]
fn a_second_client_is_turned_away_and_the_first_kept() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let first = h.capturing(k);
    let second = h.client();
    assert!(matches!(
        second.hello(),
        ToClient::Goodbye {
            reason: Goodbye::Busy,
            ..
        }
    ));
    assert_eq!(second.recv(MARGIN), Got::Closed);
    first.send(&FromClient::Heartbeat { counter: 9 });
    let got = first.until(MARGIN, |m| matches!(m, ToClient::HeartbeatEcho { .. }));
    assert_eq!(got.last(), Some(&ToClient::HeartbeatEcho { counter: 9 }));
    assert!(h.w.grabbed(k));
}

#[test]
fn a_connection_that_says_nothing_is_closed() {
    let h = Helper::start();
    let c = h.client();
    let got = c.until_closed(HELLO_TIMEOUT + MARGIN);
    assert!(matches!(
        got[0],
        ToClient::Goodbye {
            reason: Goodbye::Silent,
            ..
        }
    ));
}

#[test]
fn a_refused_caller_is_told_why() {
    let h = Helper::start_with(|_| Err((Goodbye::NotAuthorized, "polkit says no".into())));
    let c = h.client();
    assert_eq!(
        c.hello(),
        ToClient::Goodbye {
            reason: Goodbye::NotAuthorized,
            version: PROTOCOL_VERSION,
            text: "polkit says no".into()
        }
    );
    assert_eq!(c.recv(MARGIN), Got::Closed);
}

#[test]
fn a_client_that_does_not_read_is_dropped_and_everything_let_go() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    // Heartbeats are echoed; never reading the echoes fills the socket, then the helper's queue.
    let mut sent = 0;
    let start = Instant::now();
    while h.w.any_grabbed() {
        if !c.try_send(&FromClient::Heartbeat { counter: sent }) {
            thread::sleep(Duration::from_millis(1));
        }
        sent += 1;
        assert!(start.elapsed() < Duration::from_secs(20), "never dropped");
    }
    assert!(sent as usize > OUTBOX_LIMIT);
    eventually(MARGIN, "let go", || {
        h.w.virtual_down().is_empty() && !h.w.is_open(k)
    });
    // What it did get ends with the connection closed.
    loop {
        match c.recv(MARGIN) {
            Got::Message(_) => {}
            Got::Closed => break,
            Got::Nothing => panic!("still open"),
        }
    }
}

#[test]
fn the_watchdog_lets_go_when_the_loop_hangs() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    h.w.hang_next_read(STALL_LIMIT * 3);
    let hung = Instant::now();
    h.w.press(k, KEY_A);
    eventually(STALL_LIMIT + MARGIN, "the watchdog", || {
        h.ended.load(Relaxed)
    });
    let after = hung.elapsed();
    assert!(after > STALL_LIMIT, "{after:?}");
    assert!(h.w.released_by_guard());
    assert!(!h.w.any_grabbed());
    drop(c);
}

#[test]
fn a_caller_that_has_gone_is_not_asked_about() {
    let asked = Arc::new(AtomicUsize::new(0));
    let a = asked.clone();
    let h = Helper::start_with(move |_| {
        a.fetch_add(1, Relaxed);
        thread::sleep(Duration::from_millis(300));
        Ok(SESSION.into())
    });
    for _ in 0..MAX_CALLERS {
        let c = h.client();
        c.send(&FromClient::Hello {
            version: PROTOCOL_VERSION,
            features: 0,
        });
        // Long enough for the loop to put the question in line before the caller hangs up.
        thread::sleep(Duration::from_millis(20));
    }
    // The first was being asked about as the rest hung up; theirs are skipped, and the rightful
    // client is admitted once the first answer is in. Until then it is turned away as one too
    // many, and tries again.
    let deadline = Instant::now() + Duration::from_millis(300) + MARGIN;
    loop {
        let c = h.client();
        if let Got::Nothing = c.recv(Duration::from_millis(50)) {
            assert!(matches!(c.hello(), ToClient::Welcome { .. }));
            break;
        }
        assert!(Instant::now() < deadline, "not admitted in time");
    }
    assert_eq!(asked.load(Relaxed), 2);
}

/// Connections from any number of processes at once, each turned away.
fn flood(h: &Helper) -> (Arc<AtomicBool>, Vec<JoinHandle<()>>) {
    let stop = Arc::new(AtomicBool::new(false));
    let flooders = (0..8)
        .map(|_| {
            let stop = stop.clone();
            let socket = h.socket.clone();
            thread::spawn(move || {
                while !stop.load(Relaxed) {
                    let _ = Client::connect(&socket);
                }
            })
        })
        .collect();
    (stop, flooders)
}

#[test]
fn keys_go_round_in_time_through_a_flood_of_connections() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    let (stop, flooders) = flood(&h);
    let mut took = Vec::new();
    for _ in 0..10 {
        let pressed = Instant::now();
        h.w.press(k, KEY_A);
        let got = c.until(MARGIN, |m| key(m).is_some());
        c.send(&FromClient::Verdict {
            seq: key(got.last().unwrap()).unwrap().seq,
            keep: false,
        });
        eventually(MARGIN, "the key out", || h.w.virtual_down().contains(KEY_A));
        took.push(pressed.elapsed());
        h.w.release(k, KEY_A);
        c.until(MARGIN, |m| key(m).is_some_and(|k| k.value == KEY_RELEASE));
        eventually(MARGIN, "the release out", || h.w.virtual_down().is_empty());
    }
    stop.store(true, Relaxed);
    for f in flooders {
        let _ = f.join();
    }
    assert!(!h.ended.load(Relaxed));
    took.sort();
    assert!(took[took.len() / 2] < VERDICT_TIMEOUT, "{took:?}");
}

// Found in the security review of admission. It fails on purpose and says in its message what goes
// wrong.

/// Any local user may connect, and each connection is accepted, logged and turned away in the
/// loop that reads the keyboards. The accept loop in Server::accept runs until the backlog is
/// empty, so callers who connect faster than one thread turns them away keep it there.
#[test]
fn review_a_flood_of_connections_does_not_stop_the_loop() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    let stop = Arc::new(AtomicBool::new(false));
    let flooders: Vec<_> = (0..8)
        .map(|_| {
            let stop = stop.clone();
            let socket = h.socket.clone();
            thread::spawn(move || {
                while !stop.load(Relaxed) {
                    let _ = Client::connect(&socket);
                }
            })
        })
        .collect();
    let flood = Instant::now();
    let mut stalled = None;
    while flood.elapsed() < Duration::from_secs(3) {
        c.send(&FromClient::Heartbeat { counter: 1 });
        if h.ended.load(Relaxed) {
            stalled = Some(flood.elapsed());
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
    stop.store(true, Relaxed);
    for f in flooders {
        let _ = f.join();
    }
    assert!(
        stalled.is_none(),
        "connections from another process kept the loop from turning for STALL_LIMIT, and the \
         watchdog ended the helper {stalled:?} into the flood"
    );
}
