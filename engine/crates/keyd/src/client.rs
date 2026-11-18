//! A small client of the helper, for the tests: the protocol's other end over a real socket.

use std::io;
use std::os::fd::OwnedFd;
use std::path::Path;
use std::time::{Duration, Instant};

use rustix::event::{PollFd, PollFlags, Timespec};
use rustix::net::{AddressFamily, RecvFlags, SendFlags, SocketAddrUnix, SocketType};

use crate::protocol::{FromClient, MAX_MESSAGE, PROTOCOL_VERSION, ToClient};

pub struct Client {
    fd: OwnedFd,
}

/// What came from the helper.
#[derive(Debug, PartialEq, Eq)]
pub enum Got {
    Message(ToClient),
    Closed,
    Nothing,
}

impl Client {
    pub fn connect(path: &Path) -> io::Result<Client> {
        let fd = rustix::net::socket(AddressFamily::UNIX, SocketType::SEQPACKET, None)?;
        rustix::net::connect(&fd, &SocketAddrUnix::new(path)?)?;
        Ok(Client { fd })
    }

    pub fn send(&self, m: &FromClient) {
        self.send_raw(&m.encode());
    }

    pub fn send_raw(&self, b: &[u8]) {
        rustix::net::send(&self.fd, b, SendFlags::NOSIGNAL).expect("sent");
    }

    /// Sends without waiting; false when the socket is full or closed.
    pub fn try_send(&self, m: &FromClient) -> bool {
        rustix::net::send(
            &self.fd,
            &m.encode(),
            SendFlags::DONTWAIT | SendFlags::NOSIGNAL,
        )
        .is_ok()
    }

    pub fn hello(&self) -> ToClient {
        self.send(&FromClient::Hello {
            version: PROTOCOL_VERSION,
            features: 0,
        });
        match self.recv(Duration::from_secs(5)) {
            Got::Message(m) => m,
            other => panic!("no answer to HELLO: {other:?}"),
        }
    }

    pub fn recv(&self, timeout: Duration) -> Got {
        let mut fds = [PollFd::new(&self.fd, PollFlags::IN)];
        let t = Timespec {
            tv_sec: timeout.as_secs() as _,
            tv_nsec: timeout.subsec_nanos() as _,
        };
        match rustix::event::poll(&mut fds, Some(&t)) {
            Ok(0) => return Got::Nothing,
            Ok(_) => {}
            Err(e) => panic!("poll: {e}"),
        }
        let mut buf = [0u8; MAX_MESSAGE + 1];
        match rustix::net::recv(&self.fd, &mut buf, RecvFlags::DONTWAIT) {
            Ok((_, 0)) => Got::Closed,
            Ok((_, n)) => Got::Message(ToClient::decode(&buf[..n]).expect("a well-formed message")),
            Err(rustix::io::Errno::CONNRESET) => Got::Closed,
            Err(e) => panic!("recv: {e}"),
        }
    }

    /// Reads until `want` matches a message or the time is up; everything read on the way.
    pub fn until(&self, timeout: Duration, want: impl Fn(&ToClient) -> bool) -> Vec<ToClient> {
        let end = Instant::now() + timeout;
        let mut got = Vec::new();
        loop {
            let left = end.saturating_duration_since(Instant::now());
            match self.recv(left) {
                Got::Message(m) => {
                    let done = want(&m);
                    got.push(m);
                    if done {
                        return got;
                    }
                }
                Got::Closed => panic!("closed, after {got:?}"),
                Got::Nothing => panic!("nothing more within {timeout:?}, after {got:?}"),
            }
        }
    }

    /// Reads until the helper closes the connection; everything read on the way.
    pub fn until_closed(&self, timeout: Duration) -> Vec<ToClient> {
        let end = Instant::now() + timeout;
        let mut got = Vec::new();
        loop {
            let left = end.saturating_duration_since(Instant::now());
            match self.recv(left) {
                Got::Message(m) => got.push(m),
                Got::Closed => return got,
                Got::Nothing => panic!("still open after {timeout:?}, after {got:?}"),
            }
        }
    }
}
