//! What the unit tests share.

use std::io::Write;
use std::sync::mpsc::{RecvTimeoutError, channel};
use std::time::Duration;

/// Fails the test anywhere but inside tests/linux/rig, for a test that
/// opens a socket a live session could also be using. The variable alone
/// could be copied into another shell; the rig's sandbox is also the first
/// process and has no /run/user.
pub fn rig() {
    let inside = std::env::var("BASEDREADER_RIG").as_deref() == Ok("1")
        && std::fs::read_to_string("/proc/1/comm").is_ok_and(|c| c.trim_end() == "bwrap")
        && !std::path::Path::new("/run/user").exists();
    assert!(
        inside,
        "opens sockets, so runs only inside tests/linux/rig, as cargo test does"
    );
}

/// Runs `f` on a thread of its own and returns what it returns. A climb or
/// walk that a cycle keeps going never returns, and may take memory as it
/// goes, so a test that has not returned in five seconds ends the whole test
/// process, failed, rather than leave that thread running.
pub fn returns<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    let test = std::thread::current().name().unwrap_or("a test").to_owned();
    let (tx, rx) = channel();
    let running = std::thread::spawn(move || {
        let _ = tx.send(f());
    });
    match rx.recv_timeout(Duration::from_secs(5)) {
        Ok(v) => v,
        Err(RecvTimeoutError::Disconnected) => match running.join() {
            Err(panic) => std::panic::resume_unwind(panic),
            Ok(()) => unreachable!("the thread sends before it ends"),
        },
        Err(RecvTimeoutError::Timeout) => {
            // Past the test harness, which holds back what eprintln! writes
            // and would never print it once the process ends.
            let _ = writeln!(
                std::io::stderr(),
                "{test} did not return within five seconds"
            );
            std::process::exit(101);
        }
    }
}
