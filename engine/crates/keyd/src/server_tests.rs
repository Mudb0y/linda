//! The whole helper but the devices and the system bus: its real loop, socket, admission thread and
//! watchdog thread, with the mocked devices and a stand-in for logind and polkit, on the real
//! clock. The bounds checked are the protocol's own, with a margin for a loaded machine.

use std::path::PathBuf;
use std::sync::atomic::AtomicUsize;
use std::thread::JoinHandle;

use super::*;
use crate::client::{Client, Got};
use crate::keys::{KEY_A, KEY_BACKSPACE, KEY_ENTER, KEY_ESC, KEY_LEFTSHIFT};
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
        Helper::start_full(decide, None)
    }

    fn start_full(
        decide: impl FnMut(&Peer) -> Result<String, Refusal> + Send + 'static,
        notifier: Option<Notifier>,
    ) -> Helper {
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
        let mut server = Server::new(core, listener, notices.clone(), admit, progress, notifier);
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
        let hello = FromClient::Hello {
            version: PROTOCOL_VERSION,
            features: 0,
        };
        if c.try_send(&hello) {
            match c.recv(MARGIN) {
                Got::Message(ToClient::Welcome { .. }) => break,
                Got::Message(ToClient::Goodbye {
                    reason: Goodbye::TooMany,
                    ..
                })
                | Got::Closed => {}
                other => panic!("{other:?}"),
            }
        }
        assert!(Instant::now() < deadline, "not admitted in time");
        thread::sleep(Duration::from_millis(50));
    }
    assert_eq!(asked.load(Relaxed), 2);
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

/// Found in review: fails while the fault stands. The socket is open to every local user, and
/// Server::accept loops until no connection is waiting. Connections made as fast as it turns them
/// away keep it accepting, so the loop reads no keyboard: the key typed meanwhile reaches nobody,
/// and the watchdog lets go and ends the helper, which then cannot serve its client for as long as
/// the connections keep coming.
#[test]
fn fault_a_stream_of_connections_keeps_the_loop_from_the_keyboard() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    c.send(&FromClient::Heartbeat { counter: 1 });
    let flood = Duration::from_millis(1600);
    let until = Instant::now() + flood;
    let threads: Vec<JoinHandle<()>> = (0..32)
        .map(|_| {
            let s = h.socket.clone();
            thread::spawn(move || {
                while Instant::now() < until {
                    let _ = Client::connect(&s);
                }
            })
        })
        .collect();
    thread::sleep(Duration::from_millis(200));
    let pressed = Instant::now();
    h.w.press(k, KEY_A);
    let mut reached = None;
    while reached.is_none() && Instant::now() < until {
        match c.recv(Duration::from_millis(50)) {
            Got::Message(ToClient::Key(_)) => reached = Some(pressed.elapsed()),
            Got::Closed => break,
            _ => {}
        }
    }
    let ended = h.ended.load(Relaxed);
    // The loop is let back to its work before anything is judged, so that the helper can stop.
    for t in threads {
        t.join().unwrap();
    }
    assert!(
        reached.is_some_and(|d| d < VERDICT_TIMEOUT + MARGIN) && !ended,
        "while connections kept coming the key reached the client after {reached:?} (None: not in \
         {flood:?}), and the watchdog ended the helper: {ended}"
    );
}

#[test]
fn going_to_sleep_lets_go_before_the_inhibitor_is_released() {
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    let (held, inhibitor) = rustix::pipe::pipe().unwrap();
    h.notices.post(Notice::Sleep {
        asleep: true,
        inhibitor: Some(inhibitor),
    });
    let mut b = [0u8; 1];
    let read = rustix::io::read(&held, &mut b).unwrap();
    assert_eq!(read, 0, "the inhibitor is released");
    assert!(
        !h.w.any_grabbed(),
        "released before the keyboards were let go"
    );
    c.until(MARGIN, |m| {
        *m == ToClient::Released {
            reason: Released::Background,
        }
    });
    c.send(&FromClient::Heartbeat { counter: 1 });
    h.notices.post(Notice::Sleep {
        asleep: false,
        inhibitor: None,
    });
    c.until(MARGIN, |m| {
        matches!(m, ToClient::Device { device, status: DeviceStatus::Held, .. } if *device == k)
    });
}

#[test]
fn nothing_is_grabbed_after_waking_until_systemd_has_the_units_watchdog_back() {
    use std::os::unix::net::UnixDatagram;
    let dir = std::env::temp_dir().join(format!("keyd-notify-wake-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir(&dir).unwrap();
    let path = dir.join("notify");
    let systemd = UnixDatagram::bind(&path).unwrap();
    systemd.set_nonblocking(true).unwrap();
    let n = Notifier::connect(
        path.as_os_str().as_encoded_bytes(),
        Some(Duration::from_secs(2)),
    )
    .unwrap();
    let h = Helper::start_full(|_| Ok(SESSION.into()), Some(n));
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    h.notices.post(Notice::Sleep {
        asleep: true,
        inhibitor: None,
    });
    c.until(MARGIN, |m| {
        *m == ToClient::Released {
            reason: Released::Background,
        }
    });
    // systemd's queue is full as the machine wakes.
    let filler = UnixDatagram::unbound().unwrap();
    filler.set_nonblocking(true).unwrap();
    while filler.send_to(b"X", &path).is_ok() {}
    h.notices.post(Notice::Sleep {
        asleep: false,
        inhibitor: None,
    });
    for _ in 0..10 {
        c.send(&FromClient::Heartbeat { counter: 1 });
        thread::sleep(Duration::from_millis(100));
        assert!(
            !h.w.grabbed(k),
            "grabbed while systemd's watchdog was ten minutes"
        );
    }
    let mut b = [0u8; 64];
    let mut said = Vec::new();
    let mut read = |said: &mut Vec<String>| {
        while let Ok(len) = systemd.recv(&mut b) {
            said.push(String::from_utf8_lossy(&b[..len]).into_owned());
        }
    };
    read(&mut said);
    c.send(&FromClient::Heartbeat { counter: 2 });
    c.until(MARGIN * 2, |m| {
        matches!(m, ToClient::Device { device, status: DeviceStatus::Held, .. } if *device == k)
    });
    read(&mut said);
    assert!(
        said.iter().any(|m| m == "WATCHDOG_USEC=2000000"),
        "{:?}",
        said.iter().filter(|m| *m != "X").collect::<Vec<_>>()
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn only_a_poll_that_came_back_late_is_a_freeze() {
    let t = Instant::now();
    let ms = Duration::from_millis;
    // Asked to wake in 200 ms, woken 3 s later: frozen for 2.8 s.
    assert_eq!(
        frozen_for(Some(t + ms(200)), t, t + ms(3000)),
        Some(ms(2800))
    );
    assert_eq!(frozen_for(Some(t + ms(200)), t, t + ms(600)), None);
    // A deadline already past when the poll began counts from the poll's start: a loop that was
    // late getting there, stalled in its own work, gains nothing.
    assert_eq!(frozen_for(Some(t - ms(5000)), t, t + ms(10)), None);
    // Nothing asked of the clock, nothing to judge.
    assert_eq!(frozen_for(None, t, t + ms(10_000)), None);
}

// Standard error that does not take what is written to it: under systemd it is a socket to the
// journal, which stops reading when journald does. Each of these runs a test of this binary in a
// process of its own whose standard error is such a socket, filled first, so that any write to it
// that waits waits for ever.

/// The test named run by itself with standard error stalled; its exit status, or None when it had
/// not ended within `within`.
fn with_stalled_stderr(test: &str, within: Duration) -> Option<i32> {
    use std::process::{Command, Stdio};
    let (ours, theirs) = rustix::net::socketpair(
        AddressFamily::UNIX,
        SocketType::STREAM,
        SocketFlags::CLOEXEC,
        None,
    )
    .unwrap();
    for chunk in [&[b'x'; 4096][..], b"x"] {
        while rustix::net::send(&theirs, chunk, SendFlags::DONTWAIT).is_ok() {}
    }
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            test,
            "--exact",
            "--ignored",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(STALLED_STDERR, "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(theirs))
        .spawn()
        .unwrap();
    let end = Instant::now() + within;
    let status = loop {
        if let Some(s) = child.try_wait().unwrap() {
            break s.code();
        }
        if Instant::now() > end {
            let _ = child.kill();
            let _ = child.wait();
            break None;
        }
        thread::sleep(Duration::from_millis(20));
    };
    drop(ours);
    status
}

const STALLED_STDERR: &str = "KEYD_TEST_STALLED_STDERR";

/// Whether this is the process with_stalled_stderr started, which then writes as the helper does.
fn stalled_stderr() -> bool {
    let ours = std::env::var_os(STALLED_STDERR).is_some();
    crate::RAW_STDERR.store(ours, Relaxed);
    ours
}

#[test]
fn with_standard_error_stalled_the_chord_still_lets_go() {
    let status = with_stalled_stderr(
        "server::tests::stalled_stderr_chord",
        VIRTUAL_SETTLE + MARGIN * 4,
    );
    assert_eq!(status, Some(0));
}

#[test]
#[ignore = "run by with_standard_error_stalled_the_chord_still_lets_go"]
fn stalled_stderr_chord() {
    if !stalled_stderr() {
        return;
    }
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let c = h.capturing(k);
    for code in [KEY_BACKSPACE, KEY_ESC, KEY_ENTER] {
        h.w.press(k, code);
    }
    c.until(MARGIN, |m| {
        *m == ToClient::Released {
            reason: Released::Emergency,
        }
    });
    eventually(MARGIN, "the emergency chord", || !h.w.any_grabbed());
}

#[test]
fn with_standard_error_stalled_the_watchdog_still_lets_go() {
    let status = with_stalled_stderr(
        "server::tests::stalled_stderr_watchdog",
        VIRTUAL_SETTLE + STALL_LIMIT + MARGIN * 4,
    );
    assert_eq!(status, Some(0));
}

#[test]
#[ignore = "run by with_standard_error_stalled_the_watchdog_still_lets_go"]
fn stalled_stderr_watchdog() {
    if !stalled_stderr() {
        return;
    }
    let h = Helper::start();
    let k = h.w.plug("keyboard");
    let _c = h.capturing(k);
    h.w.hang_next_read(STALL_LIMIT * 3);
    h.w.press(k, KEY_A);
    eventually(STALL_LIMIT + MARGIN, "the watchdog", || {
        h.ended.load(Relaxed)
    });
    assert!(h.w.released_by_guard());
}

#[test]
fn with_standard_error_stalled_a_panic_still_ends_the_process() {
    let status = with_stalled_stderr("server::tests::stalled_stderr_panic", MARGIN * 2);
    assert_eq!(status, Some(crate::PANIC_EXIT));
}

#[test]
#[ignore = "run by with_standard_error_stalled_a_panic_still_ends_the_process"]
fn stalled_stderr_panic() {
    if !stalled_stderr() {
        return;
    }
    crate::end_on_panic();
    let _ = thread::spawn(|| panic!("on purpose")).join();
}

// Found in the second review. Ignored, so that the package's checks still pass; fails with cargo
// test -p linda-keyd -- --ignored while the fault stands.

/// Every poll that comes back more than WATCHDOG_FROZEN late is taken for a freeze of the whole
/// process and given back in full to the client's silence, every time, with no limit on how much is
/// given in all. A helper merely starved of the processor, as in a storm of swapping, wakes late at
/// every turn, and a client that has hung outright keeps the keyboards for HEARTBEAT_TIMEOUT plus ten
/// late wakes, since a turn while holding counts LOOP_TICK of the silence: 12 s when every wake is a
/// second late, about 20 s just under 1.8 s late. Neither watchdog steps in below that: the watchdog
/// thread forgives its own late wakes the same way, and systemd's is fed at every turn, which comes
/// within its two seconds until a turn takes longer. This drives the core as Server::turn does, with
/// its deadlines and frozen_for.
#[test]
fn fault_a_starved_helper_lets_go_of_a_silent_client_about_when_the_heartbeat_says() {
    let w = World::new();
    let k = w.plug("keyboard");
    let mut core = Core::new(w.devices());
    let client: ConnId = 1;
    let mut now = Instant::now();
    let front = Front::Session {
        id: SESSION.into(),
        uid: 1000,
        vt: 7,
    };
    core.front_changed(front, now);
    let peer = Peer {
        pid: 4242,
        uid: 1000,
        gid: 100,
    };
    core.connected(client, peer, now);
    let hello = FromClient::Hello {
        version: PROTOCOL_VERSION,
        features: 0,
    };
    core.received(client, &hello.encode(), now);
    core.admitted(client, Ok(SESSION.into()), now);
    core.received(client, &FromClient::Start.encode(), now);
    now += VIRTUAL_SETTLE;
    core.received(client, &FromClient::Heartbeat { counter: 0 }.encode(), now);
    core.tick(now);
    assert!(w.grabbed(k));
    let _ = core.take_output();
    // From here the client says nothing, and every wake of the loop comes a second late.
    let late = Duration::from_secs(1);
    let silent = now;
    while w.grabbed(k) {
        let began = now;
        let mut deadline = core.next_wake();
        if core.holding() {
            let tick = began + LOOP_TICK;
            deadline = Some(deadline.map_or(tick, |d| d.min(tick)));
        }
        let woke = deadline.expect("a deadline while holding").max(began) + late;
        if frozen_for(deadline, began, woke).is_some() {
            core.thawed(woke);
        }
        now = woke;
        core.tick(now);
        let _ = core.take_output();
        assert!(now - silent < Duration::from_secs(120), "never let go");
    }
    let held = now - silent;
    assert!(
        held <= HEARTBEAT_TIMEOUT + 2 * late,
        "a client silent since the keyboard was grabbed kept it {:.1} s, with every wake of the loop \
         {:.1} s late, where the heartbeat allows {:.1} s",
        held.as_secs_f32(),
        late.as_secs_f32(),
        HEARTBEAT_TIMEOUT.as_secs_f32()
    );
}
