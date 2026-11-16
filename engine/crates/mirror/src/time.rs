//! Waiting without blocking the thread. The timers run on the runtime that
//! drives zbus's own connections and method timeouts, so a wait needs no
//! thread of its own and makes progress wherever those connections do.

use std::future::Future;
use std::time::{Duration, Instant};

use zruntime::{Shared, SharedRuntime, Sleep};

use crate::Error;

/// A timer that fires once `d` has passed.
pub fn sleep(d: Duration) -> Result<Sleep<Shared>, Error> {
    Ok(SharedRuntime::current()?.sleep(d))
}

/// A timer that fires at `at`.
pub fn sleep_until(at: Instant) -> Result<Sleep<Shared>, Error> {
    Ok(SharedRuntime::current()?.sleep_until(at))
}

/// Awaits `f` for at most `d`. Giving up drops `f`, and with it whatever it
/// held, such as a half-open socket. `what` names the wait in the error.
pub async fn within<F: Future>(d: Duration, what: &str, f: F) -> Result<F::Output, Error> {
    SharedRuntime::current()?
        .timeout(d, f)
        .await
        .map_err(|_| Error::TimedOut(what.to_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_wait_gives_up_at_its_limit() {
        let t = Instant::now();
        let r = zbus::block_on(within(
            Duration::from_millis(50),
            "waiting",
            std::future::pending::<()>(),
        ));
        assert!(matches!(r, Err(Error::TimedOut(ref w)) if w == "waiting"));
        assert!(t.elapsed() < Duration::from_secs(5));
        let r = zbus::block_on(within(Duration::from_secs(5), "ready", async { 7 }));
        assert_eq!(r.unwrap(), 7);
    }

    #[test]
    fn sleeping_lets_other_work_run_on_the_same_thread() {
        use futures_util::future::{Either, select};
        use std::cell::Cell;
        let ticks = Cell::new(0);
        let counter = async {
            loop {
                ticks.set(ticks.get() + 1);
                sleep(Duration::from_millis(5)).unwrap().await;
            }
        };
        let long = async { sleep(Duration::from_millis(200)).unwrap().await };
        zbus::block_on(async {
            let counter = std::pin::pin!(counter);
            let long = std::pin::pin!(long);
            assert!(matches!(select(long, counter).await, Either::Left(_)));
        });
        assert!(ticks.get() > 5, "only {} ticks", ticks.get());
    }
}
