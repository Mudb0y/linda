//! A timer future that needs no runtime: a thread sleeps and wakes it. zbus
//! does not expose its runtime's timers, and one long-lived timer per phase
//! is all the bench needs.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};
use std::time::Duration;

#[derive(Default)]
struct State {
    done: bool,
    waker: Option<Waker>,
}

pub struct Delay(Arc<Mutex<State>>);

impl Delay {
    pub fn new(d: Duration) -> Self {
        let state = Arc::new(Mutex::new(State::default()));
        let s = state.clone();
        std::thread::spawn(move || {
            std::thread::sleep(d);
            let mut st = s.lock().expect("delay state");
            st.done = true;
            if let Some(w) = st.waker.take() {
                w.wake();
            }
        });
        Delay(state)
    }
}

impl Future for Delay {
    type Output = ();
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        let mut st = self.0.lock().expect("delay state");
        if st.done {
            Poll::Ready(())
        } else {
            st.waker = Some(cx.waker().clone());
            Poll::Pending
        }
    }
}

pub async fn sleep(d: Duration) {
    Delay::new(d).await
}
