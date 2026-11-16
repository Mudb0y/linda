//! What the unit tests share.

use std::io::Write;
use std::sync::mpsc::{RecvTimeoutError, channel};
use std::time::Duration;

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
