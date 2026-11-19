//! The helper's loop: one thread that polls the socket, the connections, the devices and the
//! notices of the other threads, hands what comes to [`Core`], and carries out what it asks for.
//! No call here blocks: every socket is non-blocking, and a client that does not read what it is
//! sent is dropped once OUTBOX_LIMIT messages wait for it.

use std::collections::{BTreeMap, VecDeque};
use std::fs;
use std::io;
use std::os::fd::{AsFd, FromRawFd, OwnedFd};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use rustix::event::{PollFd, PollFlags, Timespec};
use rustix::net::{AddressFamily, RecvFlags, SendFlags, SocketAddrUnix, SocketFlags, SocketType};

use crate::core::{ConnId, Core, Front, Output, Peer, Refusal, Source};
use crate::device::Devices;
use crate::protocol::{Goodbye, LOOP_TICK, MAX_MESSAGE, OUTBOX_LIMIT};
use crate::watchdog::{Notifier, Progress};

/// What the other threads tell the loop.
#[derive(Debug)]
pub enum Notice {
    Front(Front),
    /// The kernel put this console in front.
    Console(u32),
    Admitted(ConnId, Result<String, Refusal>),
    /// Nothing but a wake, as when the loop is to stop.
    Wake,
}

pub struct Notices {
    queue: Mutex<VecDeque<Notice>>,
    wake: OwnedFd,
}

impl Notices {
    pub fn new() -> io::Result<Arc<Notices>> {
        let wake = rustix::event::eventfd(
            0,
            rustix::event::EventfdFlags::CLOEXEC | rustix::event::EventfdFlags::NONBLOCK,
        )?;
        Ok(Arc::new(Notices {
            queue: Mutex::new(VecDeque::new()),
            wake,
        }))
    }

    pub fn post(&self, n: Notice) {
        self.queue
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .push_back(n);
        let _ = rustix::io::write(&self.wake, &1u64.to_ne_bytes());
    }

    fn drain(&self) -> Vec<Notice> {
        let mut b = [0u8; 8];
        let _ = rustix::io::read(&self.wake, &mut b);
        self.queue
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .drain(..)
            .collect()
    }
}

/// Starts the thread that asks logind and polkit about each caller with `decide`, one at a time,
/// and posts each answer.
pub fn spawn_admitter<F>(
    notices: Arc<Notices>,
    mut decide: F,
) -> io::Result<mpsc::Sender<(ConnId, Peer)>>
where
    F: FnMut(&Peer) -> Result<String, Refusal> + Send + 'static,
{
    let (tx, rx) = mpsc::channel::<(ConnId, Peer)>();
    thread::Builder::new()
        .name("admission".into())
        .spawn(move || {
            for (conn, peer) in rx {
                let answer = decide(&peer);
                notices.post(Notice::Admitted(conn, answer));
            }
        })?;
    Ok(tx)
}

/// The socket systemd handed over, when the helper was started by socket activation, or a new one
/// bound at `path`.
pub fn listener(path: &Path) -> io::Result<OwnedFd> {
    let ours = std::env::var("LISTEN_PID")
        .ok()
        .and_then(|p| p.parse::<u32>().ok())
        == Some(std::process::id());
    if ours && std::env::var("LISTEN_FDS").as_deref() == Ok("1") {
        // SAFETY: systemd passes the first socket as descriptor 3 and nothing else has taken it.
        let fd = unsafe { OwnedFd::from_raw_fd(3) };
        if rustix::net::sockopt::socket_type(&fd)? != SocketType::SEQPACKET {
            return Err(io::Error::other(
                "the socket systemd passed is not a sequenced-packet socket",
            ));
        }
        rustix::io::ioctl_fionbio(&fd, true)?;
        return Ok(fd);
    }
    bind(path)
}

/// Binds a listening socket at `path`, open to every local user: who may be the client is decided
/// by admission, not by the socket's mode.
pub fn bind(path: &Path) -> io::Result<OwnedFd> {
    let fd = rustix::net::socket_with(
        AddressFamily::UNIX,
        SocketType::SEQPACKET,
        SocketFlags::NONBLOCK | SocketFlags::CLOEXEC,
        None,
    )?;
    if fs::symlink_metadata(path).is_ok_and(|m| m.file_type().is_socket()) {
        fs::remove_file(path)?;
    }
    rustix::net::bind(&fd, &SocketAddrUnix::new(path)?)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o666))?;
    rustix::net::listen(&fd, 8)?;
    Ok(fd)
}

struct Conn {
    fd: OwnedFd,
    backlog: VecDeque<Vec<u8>>,
}

#[derive(Clone, Copy)]
enum Ready {
    Listener,
    Notices,
    Core(Source),
    Conn(ConnId),
}

pub struct Server<D: Devices> {
    core: Core<D>,
    listener: OwnedFd,
    conns: BTreeMap<ConnId, Conn>,
    next_conn: ConnId,
    notices: Arc<Notices>,
    admit: mpsc::Sender<(ConnId, Peer)>,
    progress: Arc<Progress>,
    notifier: Option<Notifier>,
    stop: Arc<AtomicBool>,
}

impl<D: Devices> Server<D> {
    pub fn new(
        core: Core<D>,
        listener: OwnedFd,
        notices: Arc<Notices>,
        admit: mpsc::Sender<(ConnId, Peer)>,
        progress: Arc<Progress>,
        notifier: Option<Notifier>,
    ) -> Self {
        Server {
            core,
            listener,
            conns: BTreeMap::new(),
            next_conn: 1,
            notices,
            admit,
            progress,
            notifier,
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Set, and a notice posted, to make [`Server::run`] return.
    pub fn stopper(&self) -> Arc<AtomicBool> {
        self.stop.clone()
    }

    pub fn run(&mut self) -> io::Result<()> {
        if let Some(n) = &self.notifier {
            n.send("READY=1");
        }
        self.progress.turned(Instant::now(), None);
        while !self.stop.load(Relaxed) {
            self.turn()?;
        }
        Ok(())
    }

    fn turn(&mut self) -> io::Result<()> {
        let now = Instant::now();
        let mut deadline = self.core.next_wake();
        let mut sooner = |t: Instant| deadline = Some(deadline.map_or(t, |d| d.min(t)));
        if self.core.holding() {
            sooner(now + LOOP_TICK);
        }
        if let Some(t) = self.notifier.as_ref().and_then(|n| n.next_feed()) {
            sooner(t);
        }
        let ready = self.poll(deadline.map(|d| d.saturating_duration_since(now)))?;
        let now = Instant::now();
        // Before anything that could grab: the loop may have slept long while it held nothing,
        // which the watchdog must not take for a stall once a keyboard is held.
        self.progress.turned(now, self.core.oldest_undecided());
        for (what, flags) in ready {
            self.dispatch(what, flags, now);
            self.carry_out(now);
        }
        self.core.tick(now);
        self.carry_out(now);
        self.progress
            .turned(Instant::now(), self.core.oldest_undecided());
        if let Some(n) = &mut self.notifier {
            n.feed(now);
        }
        Ok(())
    }

    fn poll(&self, timeout: Option<Duration>) -> io::Result<Vec<(Ready, PollFlags)>> {
        let mut what = vec![Ready::Listener, Ready::Notices];
        let mut fds = vec![
            PollFd::new(&self.listener, PollFlags::IN),
            PollFd::new(&self.notices.wake, PollFlags::IN),
        ];
        for (source, fd) in self.core.poll_set() {
            what.push(Ready::Core(source));
            fds.push(PollFd::from_borrowed_fd(fd, PollFlags::IN));
        }
        for (id, c) in &self.conns {
            let mut flags = PollFlags::IN;
            if !c.backlog.is_empty() {
                flags |= PollFlags::OUT;
            }
            what.push(Ready::Conn(*id));
            fds.push(PollFd::from_borrowed_fd(c.fd.as_fd(), flags));
        }
        let timeout = timeout.map(|d| Timespec {
            tv_sec: d.as_secs() as _,
            tv_nsec: d.subsec_nanos() as _,
        });
        match rustix::event::poll(&mut fds, timeout.as_ref()) {
            Ok(_) => {}
            Err(rustix::io::Errno::INTR) => return Ok(Vec::new()),
            Err(e) => return Err(e.into()),
        }
        Ok(what
            .into_iter()
            .zip(fds.iter().map(|f| f.revents()))
            .filter(|(_, r)| !r.is_empty())
            .collect())
    }

    fn dispatch(&mut self, what: Ready, flags: PollFlags, now: Instant) {
        match what {
            Ready::Listener => self.accept(now),
            Ready::Notices => {
                for n in self.notices.drain() {
                    match n {
                        Notice::Front(f) => self.core.front_changed(f, now),
                        Notice::Console(n) => self.core.console_changed(n, now),
                        Notice::Admitted(conn, r) => self.core.admitted(conn, r, now),
                        Notice::Wake => {}
                    }
                }
            }
            Ready::Core(Source::Watch) => self.core.watch_ready(now),
            Ready::Core(Source::Virtual) => self.core.virtual_ready(now),
            Ready::Core(Source::Keyboard(n)) => self.core.keyboard_ready(n, now),
            Ready::Conn(id) => {
                if flags.contains(PollFlags::OUT) {
                    self.flush(id, now);
                }
                if flags.intersects(PollFlags::IN | PollFlags::HUP | PollFlags::ERR) {
                    self.receive(id, now);
                }
            }
        }
    }

    fn accept(&mut self, now: Instant) {
        loop {
            let fd = match rustix::net::accept_with(
                &self.listener,
                SocketFlags::NONBLOCK | SocketFlags::CLOEXEC,
            ) {
                Ok(fd) => fd,
                Err(rustix::io::Errno::AGAIN) => return,
                Err(rustix::io::Errno::INTR | rustix::io::Errno::CONNABORTED) => continue,
                Err(e) => {
                    log!("accepting a connection failed: {e}");
                    return;
                }
            };
            let cred = match rustix::net::sockopt::socket_peercred(&fd) {
                Ok(c) => c,
                Err(e) => {
                    log!("a connection without credentials, closed: {e}");
                    continue;
                }
            };
            let peer = Peer {
                pid: u32::try_from(cred.pid.as_raw_pid()).unwrap_or(0),
                uid: cred.uid.as_raw(),
                gid: cred.gid.as_raw(),
            };
            let id = self.next_conn;
            self.next_conn += 1;
            self.conns.insert(
                id,
                Conn {
                    fd,
                    backlog: VecDeque::new(),
                },
            );
            self.core.connected(id, peer, now);
            self.carry_out(now);
        }
    }

    fn receive(&mut self, id: ConnId, now: Instant) {
        let mut buf = [0u8; MAX_MESSAGE + 1];
        // Bounded, so that a client sending without pause cannot keep the loop from the keyboards.
        for _ in 0..64 {
            let Some(c) = self.conns.get(&id) else {
                return;
            };
            match rustix::net::recv(&c.fd, &mut buf, RecvFlags::DONTWAIT) {
                // Nothing is ever sent empty, so this is the other end closing.
                Ok((_, 0)) => return self.gone(id, now),
                Ok((_, n)) => {
                    self.core.received(id, &buf[..n.min(buf.len())], now);
                    self.carry_out(now);
                }
                Err(rustix::io::Errno::AGAIN) => return,
                Err(rustix::io::Errno::INTR) => {}
                Err(_) => return self.gone(id, now),
            }
        }
    }

    /// What the core asks in answer is carried out by the caller's caller, with everything else.
    fn gone(&mut self, id: ConnId, now: Instant) {
        if self.conns.remove(&id).is_some() {
            self.core.disconnected(id, now);
        }
    }

    /// Sends what waits for a connection; false when the connection failed.
    fn flush(&mut self, id: ConnId, now: Instant) -> bool {
        let Some(c) = self.conns.get_mut(&id) else {
            return false;
        };
        while let Some(m) = c.backlog.front() {
            match rustix::net::send(&c.fd, m, SendFlags::DONTWAIT | SendFlags::NOSIGNAL) {
                Ok(_) => {
                    c.backlog.pop_front();
                }
                Err(rustix::io::Errno::AGAIN) => return true,
                Err(rustix::io::Errno::INTR) => {}
                Err(_) => {
                    self.gone(id, now);
                    return false;
                }
            }
        }
        true
    }

    fn carry_out(&mut self, now: Instant) {
        // Each output is dealt with in one step; dropping a connection may bring more.
        for _ in 0..64 {
            let out = self.core.take_output();
            if out.is_empty() {
                return;
            }
            for o in out {
                match o {
                    Output::Send(id, m) => {
                        let Some(c) = self.conns.get_mut(&id) else {
                            continue;
                        };
                        c.backlog.push_back(m.encode());
                        if c.backlog.len() > OUTBOX_LIMIT {
                            log!("connection {id} does not read what it is sent, so it is closed");
                            self.gone(id, now);
                        } else {
                            self.flush(id, now);
                        }
                    }
                    Output::Close(id) => {
                        self.flush(id, now);
                        if let Some(c) = self.conns.remove(&id) {
                            discard_unread(&c.fd);
                        }
                    }
                    Output::Admit(id, peer) => {
                        if self.admit.send((id, peer)).is_err() {
                            let why = "the admission thread has gone".to_owned();
                            self.core.admitted(id, Err((Goodbye::NoAnswer, why)), now);
                        }
                    }
                }
            }
        }
    }
}

/// Reads and drops what a connection about to be closed sent that was not read. Closed with messages
/// of its unread, a Unix socket makes the other end's next read fail with ECONNRESET ahead of what
/// was sent to it, so a client would lose the GOODBYE that says why. Shut for reading first, so that
/// nothing more comes between the last read and the close.
fn discard_unread(fd: &OwnedFd) {
    let _ = rustix::net::shutdown(fd, rustix::net::Shutdown::Read);
    let mut buf = [0u8; MAX_MESSAGE + 1];
    for _ in 0..OUTBOX_LIMIT {
        match rustix::net::recv(fd, &mut buf, RecvFlags::DONTWAIT) {
            Ok((_, 0)) | Err(_) => return,
            Ok(_) => {}
        }
    }
}

#[cfg(test)]
#[path = "server_tests.rs"]
mod tests;
