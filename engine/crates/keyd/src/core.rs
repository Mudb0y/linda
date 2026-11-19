//! The helper's logic, apart from sockets, threads and the clock, so that tests can drive it
//! through every path with a mocked device layer and a time of their choosing.
//!
//! The loop in [`crate::server`] calls in when a socket, a device or a timer wants attention, and
//! carries out what [`Core::take_output`] then holds. Nothing here blocks or waits: every wait is a
//! deadline that [`Core::next_wake`] reports and [`Core::tick`] acts on.
//!
//! The rules that keep a keyboard from being left dead, each made to fire by the tests:
//! - Nothing is grabbed unless an admitted client asked to capture and its session is in front,
//!   and the virtual keyboard exists, has appeared and has had time to be taken up.
//! - A keyboard is grabbed only while every key on it is up, looked at again after the grab.
//! - Every press waits at most VERDICT_TIMEOUT for its verdict, or VERDICT_WAIT_TIMEOUT once the
//!   client has answered it WAIT, and is passed on without one. A key's release and repeats follow
//!   what became of its press. No press goes out on the virtual keyboard before every press read
//!   before it has been decided, nor the release of a modifier; any other release goes out as soon
//!   as its press has, unless a press of the same key, its own or an earlier one, is still in
//!   line ahead of it.
//! - Letting go, for any reason, ends with every key the virtual keyboard holds down released and
//!   every keyboard closed, which the kernel takes as the end of the grab. It happens on STOP, on
//!   the emergency chord, when the client goes, falls silent, breaks the protocol or leaves keys
//!   unanswered, when its session leaves the front or the helper cannot tell which session is in
//!   front, and when the virtual keyboard fails. A switch of consoles counts as the session leaving
//!   the front from the moment the kernel makes it; only logind's word captures again.
//! - The emergency chord is looked for on each key as it is read, before the key goes anywhere.
//! - A keyboard that vanishes has a release put in line for every key it held down.
//! - A virtual keyboard that fails is destroyed, and the kernel releases what it held.

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::os::fd::BorrowedFd;
use std::time::Instant;

use crate::device::{Devices, Found, GrabError, InputEvent, Keyboard, ReadError, VirtualKeyboard};
use crate::keys::{
    Assessment, CHORD, EV_KEY, EV_SYN, KEY_CAPSLOCK, KEY_CNT, KEY_KP0, KeySet, LED_NUML, MODIFIERS,
    READER_MODIFIERS, SYN_DROPPED, is_virtual_key, modifies,
};
use crate::protocol::*;

/// A connection, numbered by the loop.
pub type ConnId = u64;

/// Who is at the other end of a connection, as the kernel says on the socket.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Peer {
    pub pid: u32,
    pub uid: u32,
    pub gid: u32,
}

/// Which session logind says is in front on seat0.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Front {
    /// The helper cannot tell: it has no system bus, or logind does not answer.
    Unknown,
    Nobody,
    Session {
        id: String,
        uid: u32,
        /// The console the session is on, VTNr by logind's word; 0 when it is on none.
        vt: u32,
    },
}

/// Why a caller is turned away or a client dropped, and the words for its log.
pub type Refusal = (Goodbye, String);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Output {
    Send(ConnId, ToClient),
    /// Send what is queued for the connection and close it.
    Close(ConnId),
    /// Ask logind and polkit about the process behind the connection.
    Admit(ConnId, Peer),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    Watch,
    Virtual,
    Keyboard(u32),
}

enum Virt<V> {
    Absent,
    /// Made, and waiting to appear and then to be taken up by the desktop.
    Settling {
        v: V,
        made: Instant,
        appeared: Option<Instant>,
        /// When to look again whether it has appeared.
        look: Instant,
    },
    Ready(V),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum KState {
    /// A key is down, or it has not been looked at yet: grab once all are up, looked at again at
    /// the time given.
    Waiting(Instant),
    /// Another program holds it: try again at the time given.
    Busy(Instant),
    Held,
}

struct Kbd<K> {
    k: K,
    name: String,
    state: KState,
    reported: Option<DeviceStatus>,
    /// The keys whose press has been read and put in line, and whose release has not.
    down: KeySet,
    /// Keys the kernel had down after it dropped events, which the helper had not told the client
    /// were down, other than those that modify others: not sent to the client nor passed on. They
    /// count towards the emergency chord, and their repeats and release go nowhere.
    unseen: KeySet,
    /// The kernel dropped events: the rest of what was read with the loss is not dealt with, and
    /// the kernel is asked which keys are down once it has all been read.
    resync_due: bool,
}

/// A press or release in line for the virtual keyboard.
#[derive(Debug, Clone, Copy)]
struct Entry {
    seq: u64,
    device: u32,
    code: u16,
    press: bool,
    arrival: Instant,
    /// For a press, true to keep it, false to pass it on, once known.
    verdict: Option<bool>,
    /// The client answered the press WAIT.
    waited: bool,
    /// A press the helper made up after a loss of events, of a key found down.
    made_up: bool,
}

impl Entry {
    /// When a press is passed on without its verdict.
    fn bound(&self) -> Instant {
        self.arrival
            + if self.waited {
                VERDICT_WAIT_TIMEOUT
            } else {
                VERDICT_TIMEOUT
            }
    }
}

struct Client {
    conn: ConnId,
    peer: Peer,
    session: String,
    /// It asked to capture, and has not asked to stop since, nor been stopped by the emergency
    /// chord or a failure of the virtual keyboard.
    wants: bool,
    last_heard: Instant,
    /// The features it was granted.
    features: u32,
}

struct Caller {
    peer: Peer,
    since: Instant,
    /// Since when it has been waiting for logind and polkit, and the features it asked for.
    admitting: Option<(Instant, u32)>,
    /// Its question has been put to logind and polkit. While the helper cannot tell which session
    /// is in front, it is not: each question costs the admission thread several calls on the
    /// system bus, and only a caller of the user in front can be admitted.
    asked: bool,
}

pub struct Core<D: Devices> {
    devices: D,
    virt: Virt<D::Virtual>,
    keyboards: BTreeMap<u32, Kbd<D::Keyboard>>,
    /// The devices a scan has dealt with while capturing: opened, or reported as ignored.
    known: BTreeSet<u32>,
    /// Those that could not be opened, said once in the log.
    open_failed: BTreeSet<u32>,
    capturing: bool,
    rescan_at: Option<Instant>,
    line: VecDeque<Entry>,
    /// What became of each decided press until its release: true when it was passed on.
    passed: HashMap<(u32, u16), bool>,
    /// Modifiers the helper made up after a loss of events and the client passed, which are not
    /// yet on the virtual keyboard, in the order they were told: they go there, in that order, just
    /// before the next press that goes out, and not at all if they are let go of first.
    held_back: Vec<(u32, u16)>,
    /// How many keyboards hold each key down on the virtual keyboard: a key goes down there with
    /// the first and up with the last, so a Shift held on two keyboards stays down until both let
    /// it go.
    pressed: Vec<u16>,
    leds: Option<u16>,
    /// The session that was in front when the lights were last set, whose they are.
    leds_by: Option<String>,
    next_seq: u64,
    client: Option<Client>,
    callers: BTreeMap<ConnId, Caller>,
    /// The connections whose questions to logind and polkit have not been answered yet, whether or
    /// not the caller is still there: each counts against MAX_CALLERS until its answer comes.
    asking: BTreeSet<ConnId>,
    front: Front,
    /// The console the kernel says is in front, N of ttyN in /sys/class/tty/tty0/active.
    console: Option<u32>,
    /// The kernel has put another console in front than the one of the session logind last named,
    /// and logind has said nothing since. Nothing is captured meanwhile.
    console_moved: bool,
    /// The run of unanswered presses, by when its first and its last press were read, as
    /// UNANSWERED_SPAN in the protocol defines it.
    unanswered: Option<(Instant, Instant)>,
    drop_pending: Option<Refusal>,
    virtual_broken: bool,
    /// logind has said the machine is going to sleep, and has not said it woke.
    asleep: bool,
    /// When logind last said the machine had woken.
    woke: Option<Instant>,
    /// When a freeze logind had not announced was last forgiven, as FREEZE_FORGIVEN_EVERY allows.
    forgiven: Option<Instant>,
    /// Since when refusals have been counted for the log, how many it has a line for, and how many
    /// more there were, as REFUSALS_LOGGED has it.
    refusals: Option<(Instant, usize, usize)>,
    /// What is left to deal with of a batch read from a keyboard, for letting go in the middle of
    /// it to pass on.
    batch: VecDeque<InputEvent>,
    /// The keyboard that batch was read from.
    batch_from: u32,
    out: Vec<Output>,
}

impl<D: Devices> Core<D> {
    pub fn new(devices: D) -> Self {
        Core {
            devices,
            virt: Virt::Absent,
            keyboards: BTreeMap::new(),
            known: BTreeSet::new(),
            open_failed: BTreeSet::new(),
            capturing: false,
            rescan_at: None,
            line: VecDeque::new(),
            passed: HashMap::new(),
            held_back: Vec::new(),
            pressed: vec![0; KEY_CNT],
            leds: None,
            leds_by: None,
            next_seq: 1,
            client: None,
            callers: BTreeMap::new(),
            asking: BTreeSet::new(),
            front: Front::Unknown,
            console: None,
            console_moved: false,
            unanswered: None,
            drop_pending: None,
            virtual_broken: false,
            asleep: false,
            woke: None,
            forgiven: None,
            refusals: None,
            batch: VecDeque::new(),
            batch_from: 0,
            out: Vec::new(),
        }
    }

    pub fn devices(&self) -> &D {
        &self.devices
    }

    pub fn take_output(&mut self) -> Vec<Output> {
        std::mem::take(&mut self.out)
    }

    /// Whether any keyboard is grabbed.
    pub fn holding(&self) -> bool {
        self.keyboards.values().any(|k| k.state == KState::Held)
    }

    /// When the oldest press still waiting for its verdict was read.
    pub fn oldest_undecided(&self) -> Option<Instant> {
        self.line
            .front()
            .filter(|e| e.press && e.verdict.is_none())
            .map(|e| e.arrival)
    }

    /// What the loop should poll besides its sockets.
    pub fn poll_set(&self) -> Vec<(Source, BorrowedFd<'_>)> {
        let mut set = Vec::new();
        if self.capturing
            && let Some(fd) = self.devices.watch_fd()
        {
            set.push((Source::Watch, fd));
        }
        if let Virt::Settling { v, .. } | Virt::Ready(v) = &self.virt {
            set.push((Source::Virtual, v.fd()));
        }
        for (n, k) in &self.keyboards {
            set.push((Source::Keyboard(*n), k.k.fd()));
        }
        set
    }

    /// The earliest deadline, at which [`Core::tick`] is due.
    pub fn next_wake(&self) -> Option<Instant> {
        let mut t: Option<Instant> = None;
        let mut at = |x: Instant| t = Some(t.map_or(x, |t| t.min(x)));
        if let Some(c) = &self.client {
            at(c.last_heard + HEARTBEAT_TIMEOUT);
        }
        for c in self.callers.values() {
            at(match c.admitting {
                None => c.since + HELLO_TIMEOUT,
                Some((since, _)) => since + ADMISSION_TIMEOUT,
            });
        }
        if let Virt::Settling {
            made,
            appeared,
            look,
            ..
        } = &self.virt
        {
            at(match appeared {
                Some(a) => *a + VIRTUAL_SETTLE,
                None => (*look).min(*made + VIRTUAL_APPEAR_TIMEOUT),
            });
        }
        for k in self.keyboards.values() {
            if let KState::Waiting(x) | KState::Busy(x) = k.state {
                at(x);
            }
        }
        if let Some(e) = self.line.front() {
            at(e.bound());
        }
        if self.capturing
            && let Some(r) = self.rescan_at
        {
            at(r);
        }
        if let Some((since, _, unlogged)) = self.refusals
            && unlogged > 0
        {
            at(since + REFUSAL_LOG_SPAN);
        }
        t
    }

    pub fn connected(&mut self, conn: ConnId, peer: Peer, now: Instant) {
        let gone_but_asking = self
            .asking
            .iter()
            .filter(|c| !self.callers.contains_key(c))
            .count();
        if self.callers.len() + gone_but_asking >= MAX_CALLERS {
            self.refuse(
                conn,
                Goodbye::TooMany,
                "too many connections are waiting to be admitted".into(),
                now,
            );
            return;
        }
        if let Some(why) = self.not_in_front(peer.uid) {
            self.refuse(conn, Goodbye::NotInFront, why, now);
            return;
        }
        self.callers.insert(
            conn,
            Caller {
                peer,
                since: now,
                admitting: None,
                asked: false,
            },
        );
    }

    /// Why a caller of this user cannot be admitted, when the helper can already tell.
    fn not_in_front(&self, uid: u32) -> Option<String> {
        match &self.front {
            Front::Session { uid: u, .. } if *u != uid => Some(format!(
                "user {uid} is not the user of the session in front"
            )),
            Front::Nobody => Some("no session is in front".into()),
            _ => None,
        }
    }

    /// Puts to logind and polkit the question of each caller that said HELLO and has not been
    /// asked about, once the helper can tell which session is in front.
    fn ask_waiting(&mut self) {
        if self.front == Front::Unknown {
            return;
        }
        for (conn, c) in &mut self.callers {
            if c.admitting.is_some() && !c.asked {
                c.asked = true;
                self.asking.insert(*conn);
                self.out.push(Output::Admit(*conn, c.peer));
            }
        }
    }

    /// One message from a connection.
    pub fn received(&mut self, conn: ConnId, m: &[u8], now: Instant) {
        let message = FromClient::decode(m);
        if self.client.as_ref().is_some_and(|c| c.conn == conn) {
            match message {
                Ok(msg) => self.client_said(msg, now),
                Err(e) => self.drop_later(Goodbye::Malformed, e.0),
            }
        } else if self.callers.contains_key(&conn) {
            match message {
                Ok(msg) => self.caller_said(conn, msg, now),
                Err(e) => self.refuse(conn, Goodbye::Malformed, e.0, now),
            }
        }
        self.settle(now);
    }

    /// The connection closed, failed, or stopped reading what it was sent.
    pub fn disconnected(&mut self, conn: ConnId, now: Instant) {
        if self.client.as_ref().is_some_and(|c| c.conn == conn) {
            let c = self.client.take().expect("the client was there");
            log!(
                "the client, process {} of user {}, has gone",
                c.peer.pid,
                c.peer.uid
            );
            if self.capturing {
                self.let_go(None, false, now);
            }
        }
        self.callers.remove(&conn);
        self.settle(now);
    }

    /// What logind and polkit said of a caller: its session, or why it is turned away.
    pub fn admitted(&mut self, conn: ConnId, result: Result<String, Refusal>, now: Instant) {
        self.asking.remove(&conn);
        let Some(features) = self
            .callers
            .get(&conn)
            .and_then(|c| c.admitting)
            .map(|a| a.1)
        else {
            return;
        };
        match result {
            Err((reason, text)) => self.refuse(conn, reason, text, now),
            Ok(_) if self.client_in_front() => self.refuse(
                conn,
                Goodbye::Busy,
                "another client of the session in front is being served".into(),
                now,
            ),
            Ok(session) => {
                if self.client.is_some() {
                    self.drop_client(
                        Goodbye::Replaced,
                        "a client of the session now in front took this one's place".into(),
                        now,
                    );
                }
                let caller = self.callers.remove(&conn).expect("the caller was there");
                log!(
                    "admitted process {} of user {}, of session {session}",
                    caller.peer.pid,
                    caller.peer.uid
                );
                self.client = Some(Client {
                    conn,
                    peer: caller.peer,
                    session,
                    wants: false,
                    last_heard: now,
                    features: features & FEATURES_KNOWN,
                });
                // Numbered afresh, so that the first key's number tells nothing of how many keys
                // another client had. Nothing is in line: no client, no capture.
                self.next_seq = 1;
                self.send_client(ToClient::welcome(features & FEATURES_KNOWN));
                self.tell_locks();
            }
        }
        self.settle(now);
    }

    /// What logind says is in front, each time it is asked or tells of a change.
    pub fn front_changed(&mut self, front: Front, now: Instant) {
        self.console_moved = false;
        if front != self.front {
            log!("in front on seat0: {front:?}");
            self.front = front;
        }
        let away: Vec<(ConnId, String)> = self
            .callers
            .iter()
            .filter_map(|(conn, c)| self.not_in_front(c.peer.uid).map(|why| (*conn, why)))
            .collect();
        for (conn, why) in away {
            self.refuse(conn, Goodbye::NotInFront, why, now);
        }
        self.ask_waiting();
        self.settle(now);
    }

    /// logind says the machine is about to sleep, or has woken. Before sleep the helper lets go
    /// of every keyboard, as when the session leaves the front: nothing needs holding while the
    /// machine goes down, and a helper that holds nothing cannot leave a keyboard dead however long
    /// it is frozen. On waking the client is not held to the time it was frozen, and the helper
    /// captures again, all keys up first, if the client's session is still in front.
    pub fn sleeping(&mut self, asleep: bool, now: Instant) {
        if asleep == self.asleep {
            return;
        }
        log!(
            "{}",
            if asleep {
                "the machine is going to sleep"
            } else {
                "the machine has woken"
            }
        );
        self.asleep = asleep;
        if !asleep {
            self.woke = Some(now);
            if let Some(c) = self.client.as_mut() {
                c.last_heard = now;
            }
        }
        self.settle(now);
    }

    /// The whole process was frozen, as while the machine suspended or resumed, which the loop knows
    /// by a wait that came back far later than it asked, at `now`. The client was frozen too, and is
    /// given its bounds afresh from the thaw: it is heard as of now, every press still waiting for
    /// its verdict is counted from now, and the run of unanswered presses starts again. Moving them
    /// on by the time frozen would not do: the loop measures that from the deadline it waited for,
    /// so whatever that deadline belonged to, a press's bound or the client's silence, would fall due
    /// at the very moment of the thaw, before the client could say anything. This is done always
    /// while logind has announced a sleep (until SLEEP_GRACE after the core is told the machine
    /// woke, which is once systemd has the unit's own watchdog timeout back), and
    /// otherwise once in FREEZE_FORGIVEN_EVERY, for a sleep that went round logind; never again and
    /// again, since a machine starved of the processor wakes the loop late at every turn, and a
    /// client that has hung must not keep the keyboards by it. Returns whether it was done.
    pub fn thawed(&mut self, now: Instant) -> bool {
        let announced = self.asleep || self.woke.is_some_and(|w| now <= w + SLEEP_GRACE);
        if !announced {
            if self
                .forgiven
                .is_some_and(|f| now < f + FREEZE_FORGIVEN_EVERY)
            {
                return false;
            }
            self.forgiven = Some(now);
        }
        if let Some(c) = self.client.as_mut() {
            c.last_heard = c.last_heard.max(now);
        }
        for c in self.callers.values_mut() {
            c.since = c.since.max(now);
            if let Some((t, _)) = &mut c.admitting {
                *t = (*t).max(now);
            }
        }
        for e in &mut self.line {
            e.arrival = e.arrival.max(now);
        }
        self.unanswered = None;
        // The desktop, frozen too, has its time to take the virtual keyboard up afresh, and udev
        // to make it appear.
        if let Virt::Settling {
            made,
            appeared,
            look,
            ..
        } = &mut self.virt
        {
            *made = (*made).max(now);
            *look = (*look).max(now);
            if let Some(a) = appeared {
                *a = (*a).max(now);
            }
        }
        true
    }

    /// The kernel has put console `n` in front. A console other than that of the session logind
    /// last named means the session in front has changed, whatever logind has yet to say: the
    /// helper lets go at once, and captures again only once logind speaks again.
    pub fn console_changed(&mut self, n: u32, now: Instant) {
        self.console = Some(n);
        if let Front::Session { vt, .. } = self.front
            && vt != n
            && !self.console_moved
        {
            log!("console {n} is in front, before logind has said so");
            self.console_moved = true;
        }
        self.settle(now);
    }

    /// The session logind last named as in front, while the kernel agrees: it has put no other
    /// console in front since, and the console in front is the session's own. logind learns of a
    /// switch of consoles after the kernel has made it, and the console thread and the logind
    /// thread may be heard in either order, so an answer is judged by the console logind places
    /// its session on, never by when it came.
    fn session_in_front(&self) -> Option<&str> {
        match &self.front {
            Front::Session { id, vt, .. }
                if !self.console_moved && self.console.is_none_or(|c| c == *vt) =>
            {
                Some(id)
            }
            _ => None,
        }
    }

    pub fn keyboard_ready(&mut self, n: u32, now: Instant) {
        let mut events = Vec::new();
        let (result, state) = match self.keyboards.get_mut(&n) {
            Some(k) => (k.k.read(&mut events), k.state),
            None => return,
        };
        match state {
            KState::Held => {
                self.batch = events.into();
                self.batch_from = n;
                while self.capturing && self.keyboards.get(&n).is_some_and(|k| !k.resync_due) {
                    let Some(ev) = self.batch.pop_front() else {
                        break;
                    };
                    self.event(n, ev, now);
                }
                // What was read after a loss is left alone: the kernel's answer is its state
                // after all of it, and nothing read before the answer may be dealt with after it,
                // or against the picture from before the loss.
                self.batch.clear();
                if let Some(kbd) = self.keyboards.get_mut(&n)
                    && std::mem::take(&mut kbd.resync_due)
                {
                    self.resync(n, now);
                }
            }
            // Copies of what the desktop has: a key may have gone up.
            KState::Waiting(_) if result.is_ok() => self.try_grab(n, now),
            _ => {}
        }
        match result {
            Ok(()) => {}
            Err(ReadError::Gone) => self.vanish(n, now),
            Err(ReadError::Io(e)) => {
                log!("reading keyboard {n} failed, so it is dropped: {e}");
                self.vanish(n, now);
            }
        }
        self.advance(now);
        self.settle(now);
    }

    pub fn virtual_ready(&mut self, now: Instant) {
        let r = match &mut self.virt {
            Virt::Settling { v, .. } | Virt::Ready(v) => v.read_leds(),
            Virt::Absent => return,
        };
        match r {
            Ok(Some(leds)) => self.set_leds(leds),
            Ok(None) => {}
            Err(e) => {
                log!("reading the virtual keyboard failed: {e}");
                self.virtual_broken = true;
            }
        }
        self.settle(now);
    }

    pub fn watch_ready(&mut self, now: Instant) {
        if self.devices.watch_drain() && self.capturing {
            self.rescan(now);
        }
        self.settle(now);
    }

    pub fn tick(&mut self, now: Instant) {
        if let Some(c) = &self.client
            && now >= c.last_heard + HEARTBEAT_TIMEOUT
        {
            let why = format!("no message for {} ms", HEARTBEAT_TIMEOUT.as_millis());
            self.drop_later(Goodbye::Silent, why);
        }
        let late: Vec<(ConnId, Goodbye, String)> = self
            .callers
            .iter()
            .filter_map(|(conn, c)| match c.admitting {
                None if now >= c.since + HELLO_TIMEOUT => Some((
                    *conn,
                    Goodbye::Silent,
                    format!("no HELLO within {} ms", HELLO_TIMEOUT.as_millis()),
                )),
                Some((since, _)) if now >= since + ADMISSION_TIMEOUT => Some((
                    *conn,
                    Goodbye::NoAnswer,
                    format!(
                        "logind and polkit did not answer within {} ms",
                        ADMISSION_TIMEOUT.as_millis()
                    ),
                )),
                _ => None,
            })
            .collect();
        for (conn, reason, text) in late {
            self.refuse(conn, reason, text, now);
        }
        self.end_refusal_span(now);
        self.settle_virtual(now);
        let due: Vec<u32> = self
            .keyboards
            .iter()
            .filter(|(_, k)| matches!(k.state, KState::Waiting(t) | KState::Busy(t) if now >= t))
            .map(|(n, _)| *n)
            .collect();
        for n in due {
            self.try_grab(n, now);
        }
        if self.capturing && self.rescan_at.is_some_and(|t| now >= t) {
            self.rescan(now);
        }
        self.advance(now);
        self.settle(now);
    }

    fn caller_said(&mut self, conn: ConnId, msg: FromClient, now: Instant) {
        let Some(caller) = self.callers.get(&conn) else {
            return;
        };
        match (msg, caller.admitting) {
            (FromClient::Hello { version, features }, None) => {
                if version != PROTOCOL_VERSION {
                    let why = format!(
                        "the helper speaks protocol version {PROTOCOL_VERSION}, not {version}"
                    );
                    self.refuse(conn, Goodbye::Version, why, now);
                } else if self.client_in_front() {
                    let why = "another client of the session in front is being served";
                    self.refuse(conn, Goodbye::Busy, why.into(), now);
                } else {
                    if let Some(c) = self.callers.get_mut(&conn) {
                        c.admitting = Some((now, features));
                    }
                    self.ask_waiting();
                }
            }
            (msg, _) => {
                let why = format!("{} before WELCOME", name(&msg));
                self.refuse(conn, Goodbye::Malformed, why, now);
            }
        }
    }

    fn client_said(&mut self, msg: FromClient, now: Instant) {
        let Some(c) = self.client.as_mut() else {
            return;
        };
        c.last_heard = now;
        match msg {
            FromClient::Hello { .. } => {
                self.drop_later(Goodbye::Malformed, "a second HELLO".into())
            }
            FromClient::Start => {
                if !c.wants {
                    log!("the client asks to capture");
                    c.wants = true;
                }
            }
            FromClient::Stop => {
                c.wants = false;
                if self.capturing {
                    self.let_go(Some(Released::Stopped), false, now);
                } else {
                    self.send_client(ToClient::Released {
                        reason: Released::Stopped,
                    });
                }
            }
            FromClient::Verdict { seq, keep } => self.verdict(seq, keep, now),
            FromClient::Wait { seq } if c.features & FEATURE_WAIT != 0 => self.wait(seq),
            FromClient::Wait { .. } => {
                self.drop_later(Goodbye::Malformed, "WAIT without FEATURE_WAIT".into())
            }
            FromClient::Heartbeat { counter } => {
                self.send_client(ToClient::HeartbeatEcho { counter })
            }
        }
    }

    fn verdict(&mut self, seq: u64, keep: bool, now: Instant) {
        if seq == 0 || seq >= self.next_seq {
            let why = format!("a VERDICT on key {seq}, which was never sent");
            self.drop_later(Goodbye::Malformed, why);
            return;
        }
        // A press counts as waiting for its verdict until the helper takes it out of line: at its
        // bound, or later, behind presses before it that hold the line. A verdict on a key out of
        // line, passed on without one and EXPIRED, or one no verdict was asked for, changes nothing.
        if let Some(e) = self.line.iter_mut().find(|e| e.seq == seq)
            && e.press
            && e.verdict.is_none()
        {
            e.verdict = Some(keep);
        }
        self.advance(now);
    }

    /// WAIT: the press keeps waiting, to VERDICT_WAIT_TIMEOUT from its reading. Only the first
    /// counts, and only while the press is still in line waiting for its verdict.
    fn wait(&mut self, seq: u64) {
        if seq == 0 || seq >= self.next_seq {
            let why = format!("a WAIT on key {seq}, which was never sent");
            self.drop_later(Goodbye::Malformed, why);
            return;
        }
        if let Some(e) = self.line.iter_mut().find(|e| e.seq == seq)
            && e.press
            && e.verdict.is_none()
        {
            e.waited = true;
        }
    }

    fn client_in_front(&self) -> bool {
        self.client.as_ref().is_some_and(|c| match &self.front {
            Front::Session { id, .. } => *id == c.session,
            // While it cannot be told, the client keeps its place.
            Front::Unknown => true,
            Front::Nobody => false,
        })
    }

    fn send_client(&mut self, m: ToClient) {
        if let Some(c) = &self.client {
            self.out.push(Output::Send(c.conn, m));
        }
    }

    fn refuse(&mut self, conn: ConnId, reason: Goodbye, text: String, now: Instant) {
        let caller = self.callers.remove(&conn);
        self.end_refusal_span(now);
        let (_, logged, unlogged) = self.refusals.get_or_insert((now, 0, 0));
        if *logged < REFUSALS_LOGGED {
            *logged += 1;
            match caller {
                Some(c) => log!(
                    "turned away process {} of user {}: {text}",
                    c.peer.pid,
                    c.peer.uid
                ),
                None => log!("turned away a connection: {text}"),
            }
        } else {
            *unlogged += 1;
        }
        self.out.push(Output::Send(
            conn,
            ToClient::Goodbye {
                reason,
                version: PROTOCOL_VERSION,
                text,
            },
        ));
        self.out.push(Output::Close(conn));
    }

    /// Ends the span of REFUSAL_LOG_SPAN once it is over, saying how many refusals in it had no
    /// line of their own.
    fn end_refusal_span(&mut self, now: Instant) {
        if let Some((since, _, unlogged)) = self.refusals
            && now >= since + REFUSAL_LOG_SPAN
        {
            if unlogged > 0 {
                log!(
                    "turned away {unlogged} more connections in {} s, not logged one by one",
                    REFUSAL_LOG_SPAN.as_secs()
                );
            }
            self.refusals = None;
        }
    }

    fn drop_later(&mut self, reason: Goodbye, text: String) {
        if self.drop_pending.is_none() {
            self.drop_pending = Some((reason, text));
        }
    }

    fn drop_client(&mut self, reason: Goodbye, text: String, now: Instant) {
        let Some(c) = self.client.take() else {
            return;
        };
        log!(
            "dropping the client, process {} of user {}: {text}",
            c.peer.pid,
            c.peer.uid
        );
        if self.capturing {
            self.let_go(None, false, now);
        }
        self.out.push(Output::Send(
            c.conn,
            ToClient::Goodbye {
                reason,
                version: PROTOCOL_VERSION,
                text,
            },
        ));
        self.out.push(Output::Close(c.conn));
    }

    /// Carries out what was left for after the work in hand, then captures or lets go as the
    /// client, its session and the virtual keyboard now call for.
    fn settle(&mut self, now: Instant) {
        // Each round leaves fewer things to fail, so this ends within a few.
        for _ in 0..8 {
            if let Some((reason, text)) = self.drop_pending.take() {
                self.drop_client(reason, text, now);
            }
            if self.virtual_broken {
                self.virtual_broken = false;
                self.break_virtual(now);
            }
            self.reconcile(now);
            if self.drop_pending.is_none() && !self.virtual_broken {
                return;
            }
        }
    }

    fn reconcile(&mut self, now: Instant) {
        let wanted = self.client.as_ref().is_some_and(|c| c.wants);
        let in_front = wanted
            && !self.asleep
            && self
                .client
                .as_ref()
                .is_some_and(|c| self.session_in_front() == Some(c.session.as_str()));
        if !in_front {
            if self.capturing {
                let why = if !wanted {
                    Released::Stopped
                } else if self.asleep {
                    Released::Background
                } else if self.front == Front::Unknown && !self.console_moved {
                    Released::SessionUnknown
                } else {
                    Released::Background
                };
                self.let_go(Some(why), false, now);
            }
            return;
        }
        match &self.virt {
            Virt::Absent => {
                match self.devices.create_virtual() {
                    Ok(v) => {
                        log!("made the virtual keyboard");
                        self.virt = Virt::Settling {
                            v,
                            made: now,
                            appeared: None,
                            look: now,
                        };
                        self.settle_virtual(now);
                    }
                    Err(e) => {
                        log!("could not make the virtual keyboard, so nothing is grabbed: {e}");
                        self.virtual_failed();
                    }
                }
                return;
            }
            Virt::Settling { .. } => return,
            Virt::Ready(_) => {}
        }
        if !self.capturing {
            log!("capturing");
            self.capturing = true;
            // What changed while it was in the background was kept from it.
            self.tell_locks();
            if let Err(e) = self.devices.watch(true) {
                log!("cannot watch for keyboards coming and going: {e}");
            }
            self.rescan(now);
        }
    }

    fn settle_virtual(&mut self, now: Instant) {
        let Virt::Settling {
            v,
            made,
            appeared,
            look,
        } = &mut self.virt
        else {
            return;
        };
        let overdue = now >= *made + VIRTUAL_APPEAR_TIMEOUT;
        if appeared.is_none() && (now >= *look || overdue) {
            match v.appeared() {
                Ok(true) => *appeared = Some(now),
                Ok(false) if !overdue => *look = now + KEYS_UP_RETRY,
                r => {
                    match r {
                        Err(e) => log!("the virtual keyboard did not appear: {e}"),
                        _ => log!(
                            "the virtual keyboard did not appear within {} ms",
                            VIRTUAL_APPEAR_TIMEOUT.as_millis()
                        ),
                    }
                    self.virt = Virt::Absent;
                    self.virtual_failed();
                    return;
                }
            }
        }
        if appeared.is_some_and(|a| now >= a + VIRTUAL_SETTLE)
            && let Virt::Settling { v, .. } = std::mem::replace(&mut self.virt, Virt::Absent)
        {
            log!("the virtual keyboard is ready");
            self.virt = Virt::Ready(v);
        }
    }

    /// The client is told that the virtual keyboard failed, and must START again to have it made
    /// anew.
    fn virtual_failed(&mut self) {
        if let Some(c) = self.client.as_mut()
            && c.wants
        {
            c.wants = false;
            self.send_client(ToClient::Released {
                reason: Released::VirtualKeyboard,
            });
        }
    }

    fn break_virtual(&mut self, now: Instant) {
        log!("the virtual keyboard failed; it is destroyed, and the kernel releases what it held");
        self.virt = Virt::Absent;
        self.pressed.fill(0);
        if self.capturing {
            self.let_go(Some(Released::VirtualKeyboard), false, now);
            if let Some(c) = self.client.as_mut() {
                c.wants = false;
            }
        } else {
            self.virtual_failed();
        }
    }

    fn rescan(&mut self, now: Instant) {
        self.rescan_at = Some(now + RESCAN_INTERVAL);
        let found = match self.devices.scan() {
            Ok(f) => f,
            Err(e) => {
                log!("cannot list the input devices: {e}");
                return;
            }
        };
        let present: BTreeSet<u32> = found.iter().map(|f| f.number).collect();
        let keyboards = &self.keyboards;
        self.known
            .retain(|n| present.contains(n) || keyboards.contains_key(n));
        self.open_failed.retain(|n| present.contains(n));
        for f in found {
            if self.known.contains(&f.number) {
                continue;
            }
            match f.assessment {
                Assessment::Keyboard => self.open(f, now),
                Assessment::Ignored(why) => {
                    log!("leaving alone {} ({}): {why:?}", f.number, f.name);
                    self.known.insert(f.number);
                    self.send_client(ToClient::Device {
                        device: f.number,
                        status: DeviceStatus::Ignored,
                        detail: why as u8,
                        name: f.name,
                    });
                }
                Assessment::NotKeyboard => {}
            }
        }
    }

    fn open(&mut self, f: Found, now: Instant) {
        match self.devices.open(&f) {
            Ok(k) => {
                self.known.insert(f.number);
                self.open_failed.remove(&f.number);
                self.keyboards.insert(
                    f.number,
                    Kbd {
                        k,
                        name: f.name,
                        state: KState::Waiting(now),
                        reported: None,
                        down: KeySet::new(),
                        unseen: KeySet::new(),
                        resync_due: false,
                    },
                );
                self.try_grab(f.number, now);
            }
            Err(e) => {
                if self.open_failed.insert(f.number) {
                    log!("cannot open {} ({}): {e}", f.node, f.name);
                }
            }
        }
    }

    /// Grabs a keyboard if every key on it is up, and looks again once grabbed, since a key that
    /// went down in between would stay down in the desktop.
    fn try_grab(&mut self, n: u32, now: Instant) {
        enum Outcome {
            Held,
            Waiting,
            Busy,
            Lost(String),
            /// Still grabbed, the kernel having refused to let go.
            Reopen,
        }
        let Some(kbd) = self.keyboards.get_mut(&n) else {
            return;
        };
        let mut stale = Vec::new();
        let outcome = match kbd.k.keys_down() {
            Err(e) => Outcome::Lost(e.to_string()),
            Ok(down) if !down.is_empty() => Outcome::Waiting,
            Ok(_) => {
                // Copies of what the desktop has had too.
                let _ = kbd.k.read(&mut stale);
                match kbd.k.grab() {
                    Err(GrabError::Busy) => Outcome::Busy,
                    Err(GrabError::Gone) => Outcome::Lost("it is gone".into()),
                    Err(GrabError::Io(e)) => {
                        if kbd.reported != Some(DeviceStatus::Busy) {
                            log!("grabbing keyboard {n} failed, to be tried again: {e}");
                        }
                        Outcome::Busy
                    }
                    Ok(()) => match kbd.k.keys_down() {
                        Ok(down) if down.is_empty() => Outcome::Held,
                        Ok(_) if !kbd.k.ungrab() => Outcome::Reopen,
                        Ok(_) => {
                            let _ = kbd.k.read(&mut stale);
                            Outcome::Waiting
                        }
                        Err(e) => {
                            kbd.k.ungrab();
                            Outcome::Lost(e.to_string())
                        }
                    },
                }
            }
        };
        match outcome {
            Outcome::Held => {
                self.set_state(n, KState::Held);
                self.show_leds(n);
            }
            Outcome::Waiting => self.set_state(n, KState::Waiting(now + KEYS_UP_RETRY)),
            Outcome::Busy => self.set_state(n, KState::Busy(now + BUSY_RETRY)),
            Outcome::Lost(why) => {
                log!("keyboard {n} cannot be used: {why}");
                self.vanish(n, now);
            }
            Outcome::Reopen => {
                // Closed, which lets go in the kernel, and opened again at once as a new one, its
                // events and the chord read on it no more meanwhile.
                log!("keyboard {n} would not be let go of, so it is closed and opened again");
                self.keyboards.remove(&n);
                self.known.remove(&n);
                self.rescan_at = Some(now);
            }
        }
    }

    fn set_state(&mut self, n: u32, state: KState) {
        let Some(kbd) = self.keyboards.get_mut(&n) else {
            return;
        };
        kbd.state = state;
        let status = match state {
            KState::Held => DeviceStatus::Held,
            KState::Waiting(_) => DeviceStatus::Waiting,
            KState::Busy(_) => DeviceStatus::Busy,
        };
        if kbd.reported == Some(status) {
            return;
        }
        kbd.reported = Some(status);
        log!("keyboard {n} ({}): {status:?}", kbd.name);
        let name = kbd.name.clone();
        self.send_client(ToClient::Device {
            device: n,
            status,
            detail: 0,
            name,
        });
    }

    /// A keyboard just grabbed shows the locks as the desktop has them; the first one grabbed
    /// before the desktop has said tells the helper what they are.
    fn show_leds(&mut self, n: u32) {
        let Some(kbd) = self.keyboards.get_mut(&n) else {
            return;
        };
        match self.leds {
            Some(leds) => {
                if let Err(e) = kbd.k.set_leds(leds) {
                    log!("cannot set the lights of keyboard {n}: {e}");
                }
            }
            None => {
                if let Ok(leds) = kbd.k.leds() {
                    self.leds = Some(leds);
                    self.leds_by = self.session_in_front().map(str::to_owned);
                    self.tell_locks();
                }
            }
        }
    }

    fn set_leds(&mut self, leds: u16) {
        let by = self.session_in_front().map(str::to_owned);
        if self.leds == Some(leds) && self.leds_by == by {
            return;
        }
        self.leds = Some(leds);
        self.leds_by = by;
        for (n, k) in &mut self.keyboards {
            if k.state == KState::Held
                && let Err(e) = k.k.set_leds(leds)
            {
                log!("cannot set the lights of keyboard {n}: {e}");
            }
        }
        self.tell_locks();
    }

    /// Sends the client the lights, when they are its own session's and that session is in front:
    /// a client in the background must learn nothing of the session in front, and lights another
    /// session set are not its own.
    fn tell_locks(&mut self) {
        if let (Some(leds), Some(c)) = (self.leds, &self.client)
            && self.leds_by.as_deref() == Some(c.session.as_str())
            && self.session_in_front() == Some(c.session.as_str())
        {
            self.send_client(ToClient::Locks { leds });
        }
    }

    /// Forgets a keyboard that is gone, with a release put in line for each key it held down.
    fn vanish(&mut self, n: u32, now: Instant) {
        let Some(kbd) = self.keyboards.remove(&n) else {
            return;
        };
        self.known.remove(&n);
        for code in kbd.down.iter() {
            self.enter(n, code, KEY_RELEASE, KEY_SYNTHETIC, now);
        }
        log!("keyboard {n} ({}) is gone", kbd.name);
        let name = kbd.name.clone();
        drop(kbd);
        self.send_client(ToClient::Device {
            device: n,
            status: DeviceStatus::Gone,
            detail: 0,
            name,
        });
        self.advance(now);
    }

    fn event(&mut self, n: u32, ev: InputEvent, now: Instant) {
        let Some(kbd) = self.keyboards.get_mut(&n) else {
            return;
        };
        match ev.kind {
            EV_SYN if ev.code == SYN_DROPPED => kbd.resync_due = true,
            EV_KEY => self.key(n, ev.code, ev.value, now),
            _ => {}
        }
    }

    /// After the kernel dropped events, as libevdev does: what was read after the loss has been
    /// left alone, and only the kernel's answer to which keys are down is believed, its state after
    /// all of that; asking also throws away every key event queued and not yet read. The helper's
    /// picture is made the kernel's from the difference with what it has told the client. A key
    /// told down that the kernel has up was released unseen, and gets its release now, in line
    /// before any key read after. A key the kernel has down that was not told down went down at a
    /// moment that cannot be known, so under which modifiers cannot either. If it modifies others,
    /// it is told now as a press, after those releases, asking for its verdict like any other: it
    /// does nothing by itself, and every key it could change is read after the answer, under it,
    /// as typed; left out, they would go to the program and to the client as typed alone, Control+C
    /// as C. Passed, a modifier but Caps Lock waits to go out until a key goes out under it, and
    /// Insert never goes out, being a key to the program (see advance). Any other such key is kept
    /// among the unseen, neither told nor put out: told now, it could be taken as typed under a
    /// modifier that was up when it went down; so is the numpad's Insert with Num Lock on, when
    /// it is 0. The unseen count
    /// towards the chord, which is looked for at once, since the person may be holding it for a way
    /// out of a helper that fell behind. From the answer on every key is read in order again, so
    /// whatever counts as down when a key is told really is, and was. A key tapped wholly within the
    /// loss, or after it in the same batch, is lost, at a moment when keys are lost anyway.
    fn resync(&mut self, n: u32, now: Instant) {
        let Some(kbd) = self.keyboards.get_mut(&n) else {
            return;
        };
        let down = match kbd.k.keys_down() {
            Ok(d) => d,
            Err(e) => {
                log!("keyboard {n} cannot be read after a loss of events: {e}");
                self.vanish(n, now);
                return;
            }
        };
        // With Num Lock on, the numpad's Insert is 0, a key like any other.
        let num_lock = self.leds.is_some_and(|l| l & (1 << LED_NUML) != 0);
        let mut unseen = KeySet::new();
        let mut found = Vec::new();
        for code in down
            .iter()
            .filter(|c| is_virtual_key(*c) && !kbd.down.contains(*c))
        {
            if modifies(code) && !(code == KEY_KP0 && num_lock) {
                found.push(code);
            } else {
                unseen.insert(code);
            }
        }
        kbd.unseen = unseen;
        let lost: Vec<u16> = kbd.down.iter().filter(|c| !down.contains(*c)).collect();
        for code in lost {
            if let Some(kbd) = self.keyboards.get_mut(&n) {
                kbd.down.remove(code);
            }
            self.enter(n, code, KEY_RELEASE, KEY_SYNTHETIC, now);
        }
        for code in found {
            if let Some(kbd) = self.keyboards.get_mut(&n) {
                kbd.down.insert(code);
            }
            self.enter(n, code, KEY_PRESS, KEY_ASK | KEY_SYNTHETIC, now);
        }
        if self.chord_held() {
            self.emergency(now);
        }
    }

    fn key(&mut self, n: u32, code: u16, value: i32, now: Instant) {
        let Some(kbd) = self.keyboards.get_mut(&n) else {
            return;
        };
        // A device that is not a keyboard's own could send a code the virtual keyboard cannot
        // pass on; the assessment keeps such devices out, and this keeps their keys out of line.
        if !is_virtual_key(code) {
            return;
        }
        // An unseen key's repeats and release go nowhere. A press of it is an ordinary one: either
        // its release was read first, or it is the very press the kernel's answer counted, read
        // after it, since the kernel sets a key's bit as the driver reports it and hands the event
        // to readers only at the end of the packet. A modifier told by the answer is down, and
        // such a press of it is ignored as any press of a key down.
        if kbd.unseen.contains(code) {
            if value != KEY_REPEAT.into() {
                kbd.unseen.remove(code);
            }
            if value != KEY_PRESS.into() {
                return;
            }
        }
        match value {
            1 if !kbd.down.contains(code) => {
                kbd.down.insert(code);
                if self.chord_held() {
                    self.emergency(now);
                    return;
                }
                self.enter(n, code, KEY_PRESS, KEY_ASK, now);
            }
            0 if kbd.down.contains(code) => {
                kbd.down.remove(code);
                self.enter(n, code, KEY_RELEASE, 0, now);
            }
            2 if kbd.down.contains(code) => self.enter(n, code, KEY_REPEAT, 0, now),
            // A press of a key already down, or a release or repeat of one whose press was never
            // seen: it was down before the grab, or its press was lost.
            _ => {}
        }
    }

    fn chord_held(&self) -> bool {
        CHORD.iter().all(|keys| {
            keys.iter().any(|c| {
                self.keyboards.values().any(|k| {
                    k.state == KState::Held && (k.down.contains(*c) || k.unseen.contains(*c))
                })
            })
        })
    }

    fn emergency(&mut self, now: Instant) {
        if let Some(c) = self.client.as_mut() {
            c.wants = false;
        }
        self.let_go(Some(Released::Emergency), true, now);
        log!("the emergency chord: let go of every keyboard");
    }

    /// Tells the client of a key, and puts a press or release in line. Repeats go to the client
    /// alone: the virtual keyboard repeats a key it holds down by itself, as the kernel does for
    /// any keyboard, and the X server and Wayland compositors make their own repeats anyway.
    fn enter(&mut self, device: u32, code: u16, value: u8, flags: u8, now: Instant) {
        let seq = self.next_seq;
        self.next_seq += 1;
        if value != KEY_REPEAT {
            self.line.push_back(Entry {
                seq,
                device,
                code,
                press: value == KEY_PRESS,
                arrival: now,
                verdict: None,
                waited: false,
                made_up: value == KEY_PRESS && flags & KEY_SYNTHETIC != 0,
            });
        }
        self.send_client(ToClient::Key(Key {
            seq,
            device,
            code,
            value,
            flags,
        }));
    }

    /// Puts out every key at the head of the line that is decided or overdue.
    fn advance(&mut self, now: Instant) {
        let mut give_up = false;
        while let Some(&e) = self.line.front() {
            if e.press {
                let keep = match e.verdict {
                    // A run of unanswered presses ends at a press answered in time, taken in the
                    // order the keys were read: its answer may well have come before an earlier
                    // press's bound ran out.
                    Some(keep) => {
                        self.unanswered = None;
                        keep
                    }
                    None if now >= e.bound() => {
                        let first = match self.unanswered {
                            Some((first, last)) if e.arrival <= last + UNANSWERED_GAP => first,
                            _ => e.arrival,
                        };
                        self.unanswered = Some((first, e.arrival));
                        self.send_client(ToClient::Expired { seq: e.seq });
                        if e.bound() >= first + UNANSWERED_SPAN {
                            give_up = true;
                        }
                        false
                    }
                    None => break,
                };
                self.line.pop_front();
                // A made-up Insert is the screen reader's to know of: to a program it is a key,
                // which would come typed late, under whatever modifiers the answer found with it.
                let out = !keep && !(e.made_up && READER_MODIFIERS.contains(&e.code));
                self.passed.insert((e.device, e.code), out);
                // Alone it would be a lone tap, Alt moving the focus to the menu bar, where the
                // key typed with it was lost; Caps Lock's press is what toggles the lock.
                if out && e.made_up && e.code != KEY_CAPSLOCK {
                    self.held_back.push((e.device, e.code));
                } else if out {
                    self.press_virtual(e.code);
                }
            } else {
                self.line.pop_front();
                if self.passed.remove(&(e.device, e.code)) == Some(true) {
                    let key = (e.device, e.code);
                    match self.held_back.iter().position(|h| *h == key) {
                        Some(i) => {
                            self.held_back.remove(i);
                        }
                        None => self.release_virtual(e.code),
                    }
                }
            }
        }
        self.release_early();
        if give_up {
            let why = format!(
                "presses went unanswered for {} ms",
                UNANSWERED_SPAN.as_millis()
            );
            self.drop_later(Goodbye::Unanswered, why);
        }
    }

    /// Puts out at once the release of a key whose press has gone out, rather than behind presses
    /// still waiting for their verdict, which may hold the line up to VERDICT_WAIT_TIMEOUT: a key
    /// held down that long on the virtual keyboard is repeated by the desktop, and Tab pressed once
    /// moves the focus twice. A release overtaking another key's press changes nothing for a
    /// program, unless the key is one of the MODIFIERS, which keep their place. A release whose
    /// own press, or an earlier press of the same key, is still in line ahead of it waits.
    fn release_early(&mut self) {
        let mut waiting: Vec<(u32, u16)> = Vec::new();
        let mut i = 0;
        while i < self.line.len() {
            let e = self.line[i];
            let key = (e.device, e.code);
            if e.press {
                waiting.push(key);
            } else if !waiting.contains(&key)
                && !MODIFIERS.contains(&e.code)
                && self.passed.get(&key) == Some(&true)
            {
                self.line.remove(i);
                self.passed.remove(&key);
                self.release_virtual(e.code);
                continue;
            }
            i += 1;
        }
    }

    /// Puts a press on the virtual keyboard, after the modifiers held back, which it goes out
    /// under.
    fn press_virtual(&mut self, code: u16) {
        for (_, m) in std::mem::take(&mut self.held_back) {
            self.put_down(m);
        }
        self.put_down(code);
    }

    fn put_down(&mut self, code: u16) {
        let Virt::Ready(v) = &mut self.virt else {
            return;
        };
        let held = &mut self.pressed[usize::from(code)];
        if *held == 0
            && let Err(e) = v.emit(code, 1)
        {
            log!("the virtual keyboard did not take a key: {e}");
            self.virtual_broken = true;
            return;
        }
        *held += 1;
    }

    fn release_virtual(&mut self, code: u16) {
        let held = &mut self.pressed[usize::from(code)];
        if *held == 0 {
            return;
        }
        *held -= 1;
        if *held == 0
            && let Virt::Ready(v) = &mut self.virt
            && let Err(e) = v.emit(code, 0)
        {
            log!("the virtual keyboard did not take a key: {e}");
            self.virtual_broken = true;
        }
    }

    /// Lets go of every keyboard. The line is settled as if no more verdicts were coming, every
    /// press undecided passed on, or with `drop_undecided` thrown away, and every key the virtual
    /// keyboard holds down is released at once, before any ungrab, each of which waits in the
    /// kernel for milliseconds while the desktop would go on repeating a key held. Then what is
    /// left of the batch being dealt with, when the emergency chord came in the middle of it, is
    /// passed on, and each keyboard in turn is read once more and what was read passed on, before
    /// that keyboard is ungrabbed, since what came to a keyboard still grabbed and was not read
    /// would reach nobody: only a key coming between that read and the start of the ungrab, a
    /// matter of microseconds, is so lost. Each key read is passed on as a tap, pressed and
    /// released at once, its release from the keyboard itself coming to the desktop after the
    /// ungrab for a key it never had down from it. The kernel stops giving the helper alone a
    /// keyboard's events as its ungrab begins, so what comes after reaches the desktop from the
    /// keyboard itself, in order after the taps.
    fn let_go(&mut self, why: Option<Released>, drop_undecided: bool, now: Instant) {
        if drop_undecided {
            self.line.clear();
        } else {
            for e in &mut self.line {
                if e.press && e.verdict.is_none() {
                    e.verdict = Some(false);
                }
            }
            self.advance(now);
        }
        for code in 0..KEY_CNT {
            if self.pressed[code] > 0 {
                self.pressed[code] = 0;
                if let Virt::Ready(v) = &mut self.virt
                    && v.emit(code as u16, 0).is_err()
                {
                    self.virtual_broken = true;
                }
            }
        }
        self.passed.clear();
        // Never on the virtual keyboard, they need no release, and must not go out as taps.
        self.held_back.clear();
        let rest: Vec<InputEvent> = self.batch.drain(..).collect();
        self.tap(self.batch_from, &rest);
        let mut keyboards = std::mem::take(&mut self.keyboards);
        for (n, kbd) in &mut keyboards {
            let mut events = Vec::new();
            if kbd.state == KState::Held {
                let _ = kbd.k.read(&mut events);
            }
            self.tap(*n, &events);
            kbd.k.ungrab();
        }
        drop(keyboards);
        self.known.clear();
        self.open_failed.clear();
        if self.capturing {
            self.capturing = false;
            if let Err(e) = self.devices.watch(false) {
                log!("cannot stop watching for keyboards: {e}");
            }
        }
        self.rescan_at = None;
        self.unanswered = None;
        log!(
            "let go of every keyboard: {}",
            why.map_or("the client went".to_owned(), |w| format!("{w:?}"))
        );
        if let Some(reason) = why {
            self.send_client(ToClient::Released { reason });
        }
    }
}

impl<D: Devices> Core<D> {
    /// Keys read as the helper lets go, each press passed on as a tap, with no verdict asked, and,
    /// when it went out, told to the client, if it is still there and its session in front, as a
    /// press and a release without flags, so that it knows what the program had. What follows a
    /// loss of events is left alone, as ever. Each read is of what the kernel keeps for a reader,
    /// a few dozen events, so a client not reading is sent a few dozen messages a keyboard at
    /// most, and is dropped, not waited for, once more wait for it than its outbox takes.
    fn tap(&mut self, device: u32, events: &[InputEvent]) {
        // Let go of because its session is no longer in front, the client must not learn what
        // went to the session that is.
        let tell = self
            .client
            .as_ref()
            .is_some_and(|c| self.session_in_front() == Some(c.session.as_str()));
        for e in events {
            if e.kind == EV_SYN && e.code == SYN_DROPPED {
                return;
            }
            if e.kind == EV_KEY && e.value == 1 && is_virtual_key(e.code) {
                self.press_virtual(e.code);
                let went_out = self.pressed[usize::from(e.code)] > 0;
                self.release_virtual(e.code);
                if !tell || !went_out {
                    continue;
                }
                for value in [KEY_PRESS, KEY_RELEASE] {
                    let seq = self.next_seq;
                    self.next_seq += 1;
                    self.send_client(ToClient::Key(Key {
                        seq,
                        device,
                        code: e.code,
                        value,
                        flags: 0,
                    }));
                }
            }
        }
    }
}

fn name(m: &FromClient) -> &'static str {
    match m {
        FromClient::Hello { .. } => "HELLO",
        FromClient::Start => "START",
        FromClient::Stop => "STOP",
        FromClient::Verdict { .. } => "VERDICT",
        FromClient::Wait { .. } => "WAIT",
        FromClient::Heartbeat { .. } => "HEARTBEAT",
    }
}

#[cfg(test)]
#[path = "core_tests.rs"]
mod tests;
