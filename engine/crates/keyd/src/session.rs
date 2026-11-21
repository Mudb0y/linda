//! Who may be the client, and which session is in front: logind and polkit on the system bus,
//! behind a trait so that the decision can be tested without them. Both run on threads of their
//! own, so that a slow or hung service never holds up a key.

use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::num::NonZeroUsize;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use futures_util::StreamExt;
use futures_util::future::{Either, select};
use rustix::event::{PollFd, PollFlags};
use zbus::message::Type;
use zbus::{Connection, MatchRule, MessageStream, OwnedObjectPath, OwnedVariant, Variant};

use crate::core::{Front, Peer, Refusal};
use crate::protocol::{
    DBUS_CALL_TIMEOUT, DBUS_RETRY, Goodbye, POLKIT_ACTION, REFUSAL_LOG_SPAN, REFUSALS_LOGGED,
    SESSION_RECHECK,
};

const LOGIN1: &str = "org.freedesktop.login1";
const LOGIN1_PATH: &str = "/org/freedesktop/login1";
const MANAGER: &str = "org.freedesktop.login1.Manager";
const SESSION: &str = "org.freedesktop.login1.Session";
const USER: &str = "org.freedesktop.login1.User";
const SEAT: &str = "org.freedesktop.login1.Seat";
/// The seat the helper serves. Only its real path sends property changes; /seat/self and
/// /seat/auto do not.
const SEAT0_PATH: &str = "/org/freedesktop/login1/seat/seat0";
const PROPERTIES: &str = "org.freedesktop.DBus.Properties";
const POLKIT: &str = "org.freedesktop.PolicyKit1";
const POLKIT_PATH: &str = "/org/freedesktop/PolicyKit1/Authority";
const POLKIT_AUTHORITY: &str = "org.freedesktop.PolicyKit1.Authority";
const NO_SESSION_FOR_PID: &str = "org.freedesktop.login1.NoSessionForPID";
const NO_SUCH_USER: &str = "org.freedesktop.login1.NoSuchUser";

/// A logind session, as far as admission needs it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionInfo {
    pub id: String,
    pub uid: u32,
    pub seat: String,
    pub active: bool,
    pub remote: bool,
    /// user, greeter, lock-screen, background and the rest logind knows.
    pub class: String,
}

/// What admission asks of logind and polkit. Each failure is in words, for the log.
pub trait Login {
    /// The object path of the session the process belongs to, or None when it is in none.
    fn session_of_pid(&mut self, pid: u32) -> Result<Option<String>, String>;
    /// The object path of the user's graphical session, or None when it has none.
    fn display_of_user(&mut self, uid: u32) -> Result<Option<String>, String>;
    fn session(&mut self, path: &str) -> Result<SessionInfo, String>;
    /// The session in front on seat0.
    fn front(&mut self) -> Result<Front, String>;
    /// Whether polkit allows the process the action, without asking anyone.
    fn authorized(&mut self, peer: &Peer, start_time: u64) -> Result<bool, String>;
}

/// Decides whether a peer may be the client: it must be a process of the session in front on
/// seat0, a local session of the peer's own user, and polkit must allow it the action. A process
/// outside any session, as a program started by a desktop through systemd's user manager is, is
/// taken as belonging to its user's graphical session, as polkit itself takes it. Returns the
/// session's ID.
///
/// The peer's process, user and group IDs are what the kernel gave on the socket at connect, but
/// the process is looked up by its ID only here, which can be seconds later. Its start time is read
/// before and after the questions, so a process that ends while it is being asked about is turned
/// away; but a process that ended between connect and the first reading, whose ID another process
/// took meanwhile, is judged in its place. That cannot cross users: the session must be of the
/// user ID from connect, and polkit is asked about that user ID. Judging the very process that
/// connected needs a pidfd taken at connect (SO_PEERPIDFD, Linux 6.5).
///
/// A process in a Flatpak or snap scope is turned away whatever logind and polkit would say: such a
/// program was given the socket only by a hole in its sandbox, and would use the helper to read the
/// keys typed into every other program. The admitted client's names and unit are logged, so that
/// who held the keyboard can be told afterwards.
pub fn admit(
    login: &mut dyn Login,
    peer: &Peer,
    start_time: &dyn Fn(u32) -> Option<u64>,
) -> Result<String, Refusal> {
    admit_reading(login, peer, start_time, &proc_file, &ADMISSIONS_LOGGED)
}

fn admit_reading(
    login: &mut dyn Login,
    peer: &Peer,
    start_time: &dyn Fn(u32) -> Option<u64>,
    read: &dyn Fn(u32, &str) -> io::Result<Vec<u8>>,
    allowance: &Mutex<Allowance>,
) -> Result<String, Refusal> {
    let not_in_front = |why: String| (Goodbye::NotInFront, why);
    let no_answer = |why: String| (Goodbye::NoAnswer, why);
    let Some(started) = start_time(peer.pid) else {
        return Err(not_in_front(format!("process {} has ended", peer.pid)));
    };
    // Read between the two readings of the start time, which vouch that it was this process's.
    let cgroup = read(peer.pid, "cgroup").map_err(|e| {
        not_in_front(format!(
            "cannot read the control group of process {}: {e}",
            peer.pid
        ))
    })?;
    let cgroup = String::from_utf8_lossy(&cgroup);
    if let Some(scope) = sandbox_scope(&cgroup) {
        return Err((
            Goodbye::NotAuthorized,
            format!(
                "process {} is in {:?}, a sandboxed program's scope",
                peer.pid,
                printable(scope)
            ),
        ));
    }
    let path = match login.session_of_pid(peer.pid).map_err(no_answer)? {
        Some(p) => p,
        None => login
            .display_of_user(peer.uid)
            .map_err(no_answer)?
            .ok_or_else(|| {
                not_in_front(format!(
                    "process {} is in no session, and its user has no graphical one",
                    peer.pid
                ))
            })?,
    };
    let s = login.session(&path).map_err(no_answer)?;
    if s.uid != peer.uid {
        return Err(not_in_front(format!(
            "session {} is of user {}, not of user {}",
            s.id, s.uid, peer.uid
        )));
    }
    if s.seat != "seat0" || s.remote {
        return Err(not_in_front(format!(
            "session {} is not a local session of seat0",
            s.id
        )));
    }
    // The login screen is a session of its own class, whose reading needs a design of its own.
    if s.class != "user" {
        return Err(not_in_front(format!(
            "session {} is of class {}, and only user sessions are served",
            s.id, s.class
        )));
    }
    let front = login.front().map_err(no_answer)?;
    if !s.active || !matches!(&front, Front::Session { id, .. } if *id == s.id) {
        return Err(not_in_front(format!("session {} is not in front", s.id)));
    }
    if !login.authorized(peer, started).map_err(no_answer)? {
        return Err((
            Goodbye::NotAuthorized,
            format!("polkit does not allow {POLKIT_ACTION}"),
        ));
    }
    let client = Client {
        pid: peer.pid,
        comm: read(peer.pid, "comm")
            .map(|b| {
                String::from_utf8_lossy(&b)
                    .trim_end_matches('\n')
                    .to_owned()
            })
            .ok(),
        program: read(peer.pid, "cmdline").ok().and_then(|b| first_word(&b)),
        unit: unit_of(&cgroup).map(str::to_owned),
    };
    if start_time(peer.pid) != Some(started) {
        return Err(not_in_front(format!(
            "process {} ended while it was being admitted",
            peer.pid
        )));
    }
    let unlogged = allowance
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .take(Instant::now());
    match unlogged {
        Some(0) => log!("{client}"),
        Some(n) => log!("{client} ({n} more admitted before were not logged)"),
        None => {}
    }
    Ok(s.id)
}

/// What is logged of a client as it is admitted: the names it gave itself and the unit systemd put
/// it in. Every user may read them in /proc, and none of them holds anything typed.
struct Client {
    pid: u32,
    comm: Option<String>,
    program: Option<String>,
    unit: Option<String>,
}

impl std::fmt::Display for Client {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let shown = |s: &Option<String>| match s {
            Some(s) => format!("{:?}", printable(s)),
            None => "unknown".into(),
        };
        write!(
            f,
            "process {} is {}, run as {}, in {}",
            self.pid,
            shown(&self.comm),
            shown(&self.program),
            shown(&self.unit)
        )
    }
}

/// How many admissions are logged in each REFUSAL_LOG_SPAN, for the reason REFUSALS_LOGGED is
/// there: a process of the user in front can be admitted as often as it connects, and each one
/// then turned away as busy, which the helper's own count of refusals no longer limits.
static ADMISSIONS_LOGGED: Mutex<Allowance> = Mutex::new(Allowance::new());

struct Allowance {
    /// Since when admissions have been counted, how many were logged, and how many were not.
    span: Option<(Instant, usize, usize)>,
}

impl Allowance {
    const fn new() -> Allowance {
        Allowance { span: None }
    }

    /// Whether one more may be logged now, and if so how many were not logged before it.
    fn take(&mut self, now: Instant) -> Option<usize> {
        let mut unlogged = 0;
        if let Some((since, _, n)) = self.span
            && now.duration_since(since) >= REFUSAL_LOG_SPAN
        {
            unlogged = n;
            self.span = None;
        }
        let (_, logged, n) = self.span.get_or_insert((now, 0, unlogged));
        if *logged < REFUSALS_LOGGED {
            *logged += 1;
            Some(std::mem::take(n))
        } else {
            *n += 1;
            None
        }
    }
}

/// A file of /proc/PID.
fn proc_file(pid: u32, name: &str) -> io::Result<Vec<u8>> {
    std::fs::read(format!("/proc/{pid}/{name}"))
}

/// The last part of each path in /proc/PID/cgroup: one line, "0::PATH", on the unified hierarchy,
/// and one for each hierarchy as well on a hybrid one.
fn last_parts(cgroup: &str) -> impl Iterator<Item = (&str, &str)> {
    cgroup.lines().filter_map(|line| {
        let mut fields = line.splitn(3, ':');
        let (_, controllers, path) = (fields.next()?, fields.next()?, fields.next()?);
        Some((controllers, path.rsplit('/').next()?))
    })
}

/// The unit or scope systemd put the process in, as its unified or own hierarchy names it.
fn unit_of(cgroup: &str) -> Option<&str> {
    let mut parts: Vec<_> = last_parts(cgroup).filter(|(_, p)| !p.is_empty()).collect();
    parts.sort_by_key(|(c, _)| match *c {
        "" => 0,
        "name=systemd" => 1,
        _ => 2,
    });
    parts.first().map(|(_, p)| *p)
}

/// The Flatpak or snap scope or service the process is in, if it is in one. Only the last part of
/// a path counts, which is where flatpak and snapd put the processes they start: the same words in
/// a slice above it, or in the middle of a unit's name, are not theirs.
fn sandbox_scope(cgroup: &str) -> Option<&str> {
    last_parts(cgroup)
        .map(|(_, p)| p)
        .find(|p| p.starts_with("app-flatpak-") || p.starts_with("snap."))
}

/// The first word of /proc/PID/cmdline, the program as it was started. Words are cut at spaces
/// too, since a program that sets its own title puts it all in the first argument.
fn first_word(cmdline: &[u8]) -> Option<String> {
    let first = cmdline.split(|&b| b == 0).next()?;
    let first = String::from_utf8_lossy(first);
    let word = first.split_whitespace().next()?;
    Some(word.to_owned())
}

/// Text from another process, made safe for one line of the log and kept short.
fn printable(s: &str) -> String {
    const MOST: usize = 160;
    s.chars()
        .take(MOST)
        .map(|c| if c.is_control() { '?' } else { c })
        .collect()
}

/// The process's start time in clock ticks since boot, as polkit identifies a process by.
pub fn start_time(pid: u32) -> Option<u64> {
    parse_start_time(&std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?)
}

/// Field 22 of /proc/PID/stat. The command name, field 2, is in parentheses and may itself hold
/// spaces and parentheses, so the fields are counted from the last closing one.
fn parse_start_time(stat: &str) -> Option<u64> {
    let rest = &stat[stat.rfind(')')? + 1..];
    rest.split_whitespace().nth(19)?.parse().ok()
}

/// logind and polkit on the system bus. The connection is made when first needed and made again
/// after any failure.
pub struct SystemBus {
    conn: Option<Connection>,
}

impl SystemBus {
    pub fn new() -> SystemBus {
        SystemBus { conn: None }
    }

    fn run<T, F, Fut>(&mut self, what: &str, f: F) -> Result<T, String>
    where
        F: FnOnce(Connection) -> Fut,
        Fut: Future<Output = zbus::Result<T>>,
    {
        let result = zbus::block_on(async {
            let conn = match self.conn.clone() {
                Some(c) => c,
                None => {
                    let c = within(DBUS_CALL_TIMEOUT, connect()).await??;
                    self.conn = Some(c.clone());
                    c
                }
            };
            within(DBUS_CALL_TIMEOUT, f(conn)).await?
        });
        result.map_err(|e| {
            if !matches!(e, zbus::Error::MethodError(..)) {
                self.conn = None;
            }
            format!("{what}: {e}")
        })
    }
}

impl Default for SystemBus {
    fn default() -> Self {
        SystemBus::new()
    }
}

impl Login for SystemBus {
    fn session_of_pid(&mut self, pid: u32) -> Result<Option<String>, String> {
        self.run(
            "asking logind for the session of a process",
            |c| async move {
                let reply = c
                    .call_method(
                        Some(LOGIN1),
                        LOGIN1_PATH,
                        Some(MANAGER),
                        "GetSessionByPID",
                        &pid,
                    )
                    .await;
                match reply {
                    Ok(m) => Ok(Some(m.body().deserialize::<OwnedObjectPath>()?.to_string())),
                    Err(e) if error_name(&e) == Some(NO_SESSION_FOR_PID) => Ok(None),
                    Err(e) => Err(e),
                }
            },
        )
    }

    fn display_of_user(&mut self, uid: u32) -> Result<Option<String>, String> {
        self.run("asking logind for the user's session", |c| async move {
            let user = match c
                .call_method(Some(LOGIN1), LOGIN1_PATH, Some(MANAGER), "GetUser", &uid)
                .await
            {
                Ok(m) => m.body().deserialize::<OwnedObjectPath>()?,
                Err(e) if error_name(&e) == Some(NO_SUCH_USER) => return Ok(None),
                Err(e) => return Err(e),
            };
            let (id, path): (String, OwnedObjectPath) =
                get(&c, user.as_str(), USER, "Display").await?.try_into()?;
            Ok((!id.is_empty()).then(|| path.to_string()))
        })
    }

    fn session(&mut self, path: &str) -> Result<SessionInfo, String> {
        let path = path.to_owned();
        self.run("asking logind about a session", |c| async move {
            let m = c
                .call_method(
                    Some(LOGIN1),
                    path.as_str(),
                    Some(PROPERTIES),
                    "GetAll",
                    &SESSION,
                )
                .await?;
            let mut all: HashMap<String, OwnedVariant> = m.body().deserialize()?;
            let mut take = |name: &str| {
                all.remove(name).ok_or_else(|| {
                    zbus::Error::Failure(format!("the session has no property {name}"))
                })
            };
            let id: String = take("Id")?.try_into()?;
            let (uid, _): (u32, OwnedObjectPath) = take("User")?.try_into()?;
            let (seat, _): (String, OwnedObjectPath) = take("Seat")?.try_into()?;
            let active: bool = take("Active")?.try_into()?;
            let remote: bool = take("Remote")?.try_into()?;
            let class: String = take("Class")?.try_into()?;
            Ok(SessionInfo {
                id,
                uid,
                seat,
                active,
                remote,
                class,
            })
        })
    }

    fn front(&mut self) -> Result<Front, String> {
        self.run("asking logind which session is in front", |c| async move {
            front(&c).await
        })
    }

    fn authorized(&mut self, peer: &Peer, start_time: u64) -> Result<bool, String> {
        let peer = *peer;
        self.run("asking polkit", |c| async move {
            let subject: HashMap<&str, Variant<'_>> = HashMap::from([
                ("pid", Variant::from(peer.pid)),
                ("start-time", Variant::from(start_time)),
                ("uid", Variant::from(peer.uid as i32)),
            ]);
            let details: HashMap<&str, &str> = HashMap::new();
            // No flags: polkit answers from the action's defaults and rules, and never asks the
            // user for a password on the helper's behalf.
            let m = c
                .call_method(
                    Some(POLKIT),
                    POLKIT_PATH,
                    Some(POLKIT_AUTHORITY),
                    "CheckAuthorization",
                    &(("unix-process", subject), POLKIT_ACTION, details, 0u32, ""),
                )
                .await?;
            let (authorized, _challenge, _details): (bool, bool, HashMap<String, String>) =
                m.body().deserialize()?;
            Ok(authorized)
        })
    }
}

async fn connect() -> zbus::Result<Connection> {
    zbus::connection::Builder::system()
        .method_timeout(DBUS_CALL_TIMEOUT)
        .build()
        .await
}

async fn within<T>(d: Duration, f: impl Future<Output = T>) -> zbus::Result<T> {
    zruntime::SharedRuntime::current()?
        .timeout(d, f)
        .await
        .map_err(|_| zbus::Error::InputOutput(io::Error::from(io::ErrorKind::TimedOut).into()))
}

async fn get(c: &Connection, path: &str, iface: &str, name: &str) -> zbus::Result<OwnedVariant> {
    let m = c
        .call_method(Some(LOGIN1), path, Some(PROPERTIES), "Get", &(iface, name))
        .await?;
    m.body().deserialize()
}

async fn front(c: &Connection) -> zbus::Result<Front> {
    let (id, path): (String, OwnedObjectPath) = get(c, SEAT0_PATH, SEAT, "ActiveSession")
        .await?
        .try_into()?;
    if id.is_empty() {
        return Ok(Front::Nobody);
    }
    let (uid, _): (u32, OwnedObjectPath) =
        get(c, path.as_str(), SESSION, "User").await?.try_into()?;
    let vt: u32 = get(c, path.as_str(), SESSION, "VTNr").await?.try_into()?;
    Ok(Front::Session { id, uid, vt })
}

fn error_name(e: &zbus::Error) -> Option<&str> {
    match e {
        zbus::Error::MethodError(name, _, _) => Some(name.as_str()),
        _ => None,
    }
}

/// Starts the thread that follows which session is in front on seat0 and posts what logind says
/// each time it is asked, on each change it tells of and every SESSION_RECHECK, the same answer
/// again included: after the kernel has switched consoles, the helper waits for logind to say
/// anything at all. While the system bus or logind cannot be reached it posts [`Front::Unknown`],
/// which makes the helper let go, and tries again every DBUS_RETRY.
pub fn watch_front(post: impl Fn(Front) + Send + 'static) -> io::Result<thread::JoinHandle<()>> {
    thread::Builder::new().name("logind".into()).spawn(move || {
        loop {
            let e = zbus::block_on(follow(&post));
            log!("lost track of the session in front: {e}");
            post(Front::Unknown);
            thread::sleep(DBUS_RETRY);
        }
    })
}

async fn follow(post: &dyn Fn(Front)) -> zbus::Error {
    let r: zbus::Result<()> = async {
        let conn = within(DBUS_CALL_TIMEOUT, connect()).await??;
        let rule = MatchRule::builder()
            .msg_type(Type::Signal)
            .sender(LOGIN1)
            .path(SEAT0_PATH)
            .interface(PROPERTIES)
            .member("PropertiesChanged")
            .build()?;
        // Subscribed before the first look, so that no change between the two is missed.
        let mut changes = MessageStream::for_match_rule(rule, &conn, NonZeroUsize::new(16)).await?;
        loop {
            post(within(DBUS_CALL_TIMEOUT, front(&conn)).await??);
            let timer = zruntime::SharedRuntime::current()?.sleep(SESSION_RECHECK);
            if let Either::Left((None, _)) = select(changes.next(), timer).await {
                return Err(zbus::Error::Failure("the bus connection closed".into()));
            }
        }
    }
    .await;
    match r {
        Err(e) => e,
        Ok(()) => zbus::Error::Failure("stopped".into()),
    }
}

/// Starts the thread that follows logind's PrepareForSleep and posts whether the machine is going to
/// sleep each time that changes, going to sleep with the delay inhibitor that holds the sleep back
/// until the helper has let go. It holds that inhibitor while the machine is awake, takes it again
/// on waking, and asks logind every SESSION_RECHECK as well as on each signal, so that a signal lost
/// with the bus leaves the helper asleep no longer than it takes to ask again. Without an inhibitor,
/// which polkit may refuse, the helper still lets go on the signal, but sleep need not wait for it.
pub fn watch_sleep(
    post: impl Fn(bool, Option<std::os::fd::OwnedFd>) + Send + 'static,
) -> io::Result<thread::JoinHandle<()>> {
    thread::Builder::new().name("sleep".into()).spawn(move || {
        let mut asleep = false;
        let mut told = false;
        loop {
            let e = zbus::block_on(follow_sleep(&post, &mut asleep, &mut told));
            log!("lost track of sleep: {e}");
            thread::sleep(DBUS_RETRY);
        }
    })
}

async fn follow_sleep(
    post: &dyn Fn(bool, Option<std::os::fd::OwnedFd>),
    asleep: &mut bool,
    told: &mut bool,
) -> zbus::Error {
    let r: zbus::Result<()> = async {
        let conn = within(DBUS_CALL_TIMEOUT, connect()).await??;
        let rule = MatchRule::builder()
            .msg_type(Type::Signal)
            .sender(LOGIN1)
            .path(LOGIN1_PATH)
            .interface(MANAGER)
            .member("PrepareForSleep")
            .build()?;
        // Subscribed before the first look, so that no change between the two is missed.
        let mut signals = MessageStream::for_match_rule(rule, &conn, NonZeroUsize::new(16)).await?;
        let mut inhibitor = None;
        let mut now_asleep: bool = within(
            DBUS_CALL_TIMEOUT,
            get(&conn, LOGIN1_PATH, MANAGER, "PreparingForSleep"),
        )
        .await??
        .try_into()?;
        loop {
            if !now_asleep && inhibitor.is_none() {
                match within(DBUS_CALL_TIMEOUT, inhibit(&conn)).await? {
                    Ok(fd) => inhibitor = Some(fd),
                    Err(e) if !*told => {
                        *told = true;
                        log!(
                            "logind will not wait for the helper before sleep, which it lets go \
                             of the keyboards for as soon as it hears: {e}"
                        );
                    }
                    Err(_) => {}
                }
            }
            if now_asleep != *asleep {
                *asleep = now_asleep;
                post(now_asleep, if now_asleep { inhibitor.take() } else { None });
            }
            let timer = zruntime::SharedRuntime::current()?.sleep(SESSION_RECHECK);
            now_asleep = match select(signals.next(), timer).await {
                Either::Left((None, _)) => {
                    return Err(zbus::Error::Failure("the bus connection closed".into()));
                }
                Either::Left((Some(m), _)) => m?.body().deserialize::<bool>()?,
                Either::Right(_) => within(
                    DBUS_CALL_TIMEOUT,
                    get(&conn, LOGIN1_PATH, MANAGER, "PreparingForSleep"),
                )
                .await??
                .try_into()?,
            };
        }
    }
    .await;
    match r {
        Err(e) => e,
        Ok(()) => zbus::Error::Failure("stopped".into()),
    }
}

/// A delay inhibitor for sleep: logind waits, up to its InhibitDelayMaxSec, for it to be closed.
async fn inhibit(c: &Connection) -> zbus::Result<std::os::fd::OwnedFd> {
    let m = c
        .call_method(
            Some(LOGIN1),
            LOGIN1_PATH,
            Some(MANAGER),
            "Inhibit",
            &(
                "sleep",
                "linda-keyd",
                "Lets go of the keyboards before sleep",
                "delay",
            ),
        )
        .await?;
    Ok(m.body().deserialize::<zbus::OwnedFd>()?.into())
}

/// The file naming the console in front, whose pollers the kernel notifies at every switch.
const ACTIVE_CONSOLE: &str = "/sys/class/tty/tty0/active";

/// Starts the thread that posts the number of the console in front each time the kernel switches
/// consoles, the moment it does, which is before logind has told anyone. Where there are no
/// consoles it starts nothing, and logind alone says what is in front.
pub fn watch_console(post: impl Fn(u32) + Send + 'static) -> io::Result<()> {
    let file = match std::fs::File::open(ACTIVE_CONSOLE) {
        Ok(f) => f,
        Err(e) => {
            log!("cannot follow the console in front, {ACTIVE_CONSOLE}: {e}");
            return Ok(());
        }
    };
    thread::Builder::new().name("console".into()).spawn(move || {
        use std::os::unix::fs::FileExt;
        let mut last = None;
        let mut buf = [0u8; 32];
        loop {
            // sysfs notifies a poller only of changes after its last read of the file.
            let n = match file.read_at(&mut buf, 0) {
                Ok(n) => n,
                Err(e) => {
                    log!("cannot read {ACTIVE_CONSOLE}, so the console in front is not followed: {e}");
                    return;
                }
            };
            let console = parse_console(&buf[..n]);
            if let Some(c) = console
                && console != last
            {
                last = console;
                post(c);
            }
            let mut fds = [PollFd::new(&file, PollFlags::PRI | PollFlags::ERR)];
            if let Err(e) = rustix::event::poll(&mut fds, None)
                && e != rustix::io::Errno::INTR
            {
                log!("cannot wait on {ACTIVE_CONSOLE}, so the console in front is not followed: {e}");
                return;
            }
        }
    })?;
    Ok(())
}

/// N of the "ttyN" the file holds.
fn parse_console(text: &[u8]) -> Option<u32> {
    std::str::from_utf8(text)
        .ok()?
        .trim()
        .strip_prefix("tty")?
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    const PEER: Peer = Peer {
        pid: 300,
        uid: 1000,
        gid: 100,
    };

    /// logind and polkit as a test wants them.
    struct Fake {
        by_pid: Result<Option<String>, String>,
        display: Option<String>,
        sessions: HashMap<String, SessionInfo>,
        front: Front,
        allowed: Result<bool, String>,
        asked_polkit: bool,
    }

    impl Fake {
        fn usual() -> Fake {
            let path = "/org/freedesktop/login1/session/_32".to_owned();
            Fake {
                by_pid: Ok(Some(path.clone())),
                display: None,
                sessions: HashMap::from([(
                    path,
                    SessionInfo {
                        id: "2".into(),
                        uid: 1000,
                        seat: "seat0".into(),
                        active: true,
                        remote: false,
                        class: "user".into(),
                    },
                )]),
                front: Front::Session {
                    id: "2".into(),
                    uid: 1000,
                    vt: 7,
                },
                allowed: Ok(true),
                asked_polkit: false,
            }
        }

        fn edit(&mut self, f: impl FnOnce(&mut SessionInfo)) {
            f(self.sessions.values_mut().next().unwrap());
        }
    }

    impl Login for Fake {
        fn session_of_pid(&mut self, _: u32) -> Result<Option<String>, String> {
            self.by_pid.clone()
        }
        fn display_of_user(&mut self, _: u32) -> Result<Option<String>, String> {
            Ok(self.display.clone())
        }
        fn session(&mut self, path: &str) -> Result<SessionInfo, String> {
            self.sessions.get(path).cloned().ok_or("no such".into())
        }
        fn front(&mut self) -> Result<Front, String> {
            Ok(self.front.clone())
        }
        fn authorized(&mut self, _: &Peer, start: u64) -> Result<bool, String> {
            assert_eq!(start, 77);
            self.asked_polkit = true;
            self.allowed.clone()
        }
    }

    fn started(_: u32) -> Option<u64> {
        Some(77)
    }

    #[test]
    fn a_process_of_the_session_in_front_is_admitted() {
        let mut f = Fake::usual();
        assert_eq!(admit_in(&mut f, IN_SESSION), Ok("2".into()));
        assert!(f.asked_polkit);
    }

    #[test]
    fn a_process_outside_any_session_counts_as_its_users_graphical_one() {
        let mut f = Fake::usual();
        f.display = f.by_pid.clone().unwrap();
        f.by_pid = Ok(None);
        assert_eq!(admit_in(&mut f, IN_SESSION), Ok("2".into()));
        f.display = None;
        let e = admit_in(&mut f, IN_SESSION).unwrap_err();
        assert_eq!(e.0, Goodbye::NotInFront);
    }

    #[test]
    fn anything_but_the_local_session_in_front_is_turned_away_before_polkit_is_asked() {
        type Change = Box<dyn Fn(&mut Fake)>;
        let cases: Vec<(&str, Change)> = vec![
            ("another user's", Box::new(|f| f.edit(|s| s.uid = 1001))),
            (
                "on another seat",
                Box::new(|f| f.edit(|s| s.seat = "seat1".into())),
            ),
            (
                "with no seat",
                Box::new(|f| f.edit(|s| s.seat = String::new())),
            ),
            ("remote", Box::new(|f| f.edit(|s| s.remote = true))),
            (
                "the login screen's",
                Box::new(|f| f.edit(|s| s.class = "greeter".into())),
            ),
            (
                "a lock screen's",
                Box::new(|f| f.edit(|s| s.class = "lock-screen".into())),
            ),
            ("inactive", Box::new(|f| f.edit(|s| s.active = false))),
            (
                "not in front",
                Box::new(|f| {
                    f.front = Front::Session {
                        id: "3".into(),
                        uid: 1000,
                        vt: 2,
                    }
                }),
            ),
            (
                "with nobody in front",
                Box::new(|f| f.front = Front::Nobody),
            ),
        ];
        for (what, change) in cases {
            let mut f = Fake::usual();
            change(&mut f);
            let e = admit_in(&mut f, IN_SESSION).unwrap_err();
            assert_eq!(e.0, Goodbye::NotInFront, "{what}: {}", e.1);
            assert!(!f.asked_polkit, "{what}");
        }
    }

    #[test]
    fn polkit_has_the_last_word() {
        let mut f = Fake::usual();
        f.allowed = Ok(false);
        assert_eq!(
            admit_in(&mut f, IN_SESSION).unwrap_err().0,
            Goodbye::NotAuthorized
        );
        f.allowed = Err("polkit is not running".into());
        assert_eq!(
            admit_in(&mut f, IN_SESSION).unwrap_err().0,
            Goodbye::NoAnswer
        );
    }

    #[test]
    fn a_failing_logind_is_no_answer() {
        let mut f = Fake::usual();
        f.by_pid = Err("timed out".into());
        let e = admit_in(&mut f, IN_SESSION).unwrap_err();
        assert_eq!(e, (Goodbye::NoAnswer, "timed out".into()));
    }

    #[test]
    fn a_process_that_ends_or_is_replaced_is_turned_away() {
        let mut f = Fake::usual();
        assert_eq!(
            admit(&mut f, &PEER, &|_| None).unwrap_err().0,
            Goodbye::NotInFront
        );
        let calls = std::cell::Cell::new(0);
        let changing = |_| {
            calls.set(calls.get() + 1);
            Some(if calls.get() == 1 { 77 } else { 78 })
        };
        let e = admit_reading(
            &mut f,
            &PEER,
            &changing,
            &proc_of(IN_SESSION),
            &Mutex::new(Allowance::new()),
        )
        .unwrap_err();
        assert!(e.1.contains("ended while"), "{}", e.1);
    }

    const IN_SESSION: &str = "0::/user.slice/user-1000.slice/session-2.scope\n";

    /// /proc of a process in the control group given, with the names given.
    fn proc_of(cgroup: &'static str) -> impl Fn(u32, &str) -> io::Result<Vec<u8>> {
        move |pid, name| {
            assert_eq!(pid, PEER.pid);
            Ok(match name {
                "cgroup" => cgroup.as_bytes().to_vec(),
                "comm" => b"linda\n".to_vec(),
                "cmdline" => b"/run/current-system/sw/bin/python3\0-m\0linda\0".to_vec(),
                _ => panic!("reads {name}"),
            })
        }
    }

    fn admit_in(f: &mut Fake, cgroup: &'static str) -> Result<String, Refusal> {
        admit_reading(
            f,
            &PEER,
            &started,
            &proc_of(cgroup),
            &Mutex::new(Allowance::new()),
        )
    }

    #[test]
    fn a_sandboxed_program_is_turned_away_before_anyone_is_asked() {
        let sandboxed = [
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.example.Keys-4242.scope\n",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-test-1.scope\n",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/snap.keys.keys-0d6e.scope\n",
            "0::/system.slice/snap.keys.daemon.service\n",
            // A hybrid hierarchy, where systemd's own names the scope.
            "12:cpu,cpuacct:/user.slice\n1:name=systemd:/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-x-1.scope\n0::/user.slice\n",
        ];
        for cgroup in sandboxed {
            let mut f = Fake::usual();
            f.by_pid = Err("logind must not be asked".into());
            let e = admit_in(&mut f, cgroup).unwrap_err();
            assert_eq!(e.0, Goodbye::NotAuthorized, "{cgroup}: {}", e.1);
            assert!(e.1.contains("sandboxed"), "{}", e.1);
            assert!(!f.asked_polkit);
        }
    }

    #[test]
    fn the_same_words_elsewhere_in_the_path_are_not_a_sandbox() {
        let plain = [
            IN_SESSION,
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/ground-app-flatpak-test-2.scope\n",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak.slice/app-flatpak-test.slice/ground-sliced.scope\n",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/snapper.service\n",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-linda-1234.scope\n",
            "0::/\n",
        ];
        for cgroup in plain {
            let mut f = Fake::usual();
            assert_eq!(admit_in(&mut f, cgroup), Ok("2".into()), "{cgroup}");
            assert!(f.asked_polkit);
        }
    }

    #[test]
    fn a_process_whose_control_group_cannot_be_read_is_turned_away() {
        let mut f = Fake::usual();
        let gone = |_: u32, _: &str| -> io::Result<Vec<u8>> {
            Err(io::Error::from(io::ErrorKind::NotFound))
        };
        let e = admit_reading(
            &mut f,
            &PEER,
            &started,
            &gone,
            &Mutex::new(Allowance::new()),
        )
        .unwrap_err();
        assert_eq!(e.0, Goodbye::NotInFront);
        assert!(!f.asked_polkit);
    }

    #[test]
    fn the_admitted_client_is_logged_by_its_names_and_unit() {
        let allowance = Mutex::new(Allowance::new());
        let before = crate::logged();
        let mut f = Fake::usual();
        admit_reading(&mut f, &PEER, &started, &proc_of(IN_SESSION), &allowance).unwrap();
        assert_eq!(crate::logged(), before + 1);
        // Turned away, nothing of the process is logged here; the helper logs the refusal.
        f.allowed = Ok(false);
        admit_reading(&mut f, &PEER, &started, &proc_of(IN_SESSION), &allowance).unwrap_err();
        assert_eq!(crate::logged(), before + 1);
        let client = Client {
            pid: 300,
            comm: Some("linda".into()),
            program: first_word(b"/run/current-system/sw/bin/python3\0-m\0linda\0"),
            unit: unit_of(IN_SESSION).map(str::to_owned),
        };
        assert_eq!(
            client.to_string(),
            r#"process 300 is "linda", run as "/run/current-system/sw/bin/python3", in "session-2.scope""#
        );
    }

    #[test]
    fn what_a_process_calls_itself_cannot_break_the_line() {
        let client = Client {
            pid: 300,
            comm: Some("a\nlinda-keyd: b".into()),
            program: None,
            unit: None,
        };
        let line = client.to_string();
        assert!(!line.contains('\n'), "{line}");
        assert!(line.ends_with("run as unknown, in unknown"), "{line}");
        assert_eq!(printable(&"x".repeat(1000)).len(), 160);
    }

    #[test]
    fn the_program_is_the_first_word_of_its_command_line() {
        assert_eq!(
            first_word(b"/usr/bin/python3\0/usr/bin/linda\0"),
            Some("/usr/bin/python3".into())
        );
        // A program that set its own title, with what it was given after the name.
        assert_eq!(
            first_word(b"linda --some argument\0\0\0"),
            Some("linda".into())
        );
        assert_eq!(first_word(b""), None);
        assert_eq!(first_word(b"\0"), None);
    }

    #[test]
    fn the_unit_is_the_systemd_one() {
        assert_eq!(unit_of(IN_SESSION), Some("session-2.scope"));
        assert_eq!(
            unit_of(
                "12:cpu:/user.slice\n1:name=systemd:/user.slice/user-1000.slice/session-3.scope\n0::/\n"
            ),
            Some("session-3.scope")
        );
        assert_eq!(unit_of("0::/\n"), None);
        assert_eq!(unit_of(""), None);
    }

    #[test]
    fn admissions_are_logged_ten_a_minute_and_the_rest_counted() {
        let mut a = Allowance::new();
        let t = Instant::now();
        for _ in 0..REFUSALS_LOGGED {
            assert_eq!(a.take(t), Some(0));
        }
        assert_eq!(a.take(t), None);
        assert_eq!(a.take(t + REFUSAL_LOG_SPAN / 2), None);
        let later = t + REFUSAL_LOG_SPAN;
        assert_eq!(a.take(later), Some(2));
        assert_eq!(a.take(later), Some(0));
    }

    #[test]
    fn the_console_in_front_is_read_as_the_kernel_writes_it() {
        assert_eq!(parse_console(b"tty7\n"), Some(7));
        assert_eq!(parse_console(b"tty12"), Some(12));
        assert_eq!(parse_console(b"ttyS0\n"), None);
        assert_eq!(parse_console(b""), None);
    }

    #[test]
    fn the_start_time_is_read_past_any_command_name() {
        let mut fields: Vec<String> = (3..=52).map(|n| n.to_string()).collect();
        fields[22 - 3] = "123456".into();
        let stat = format!("4242 (a (b) c) {}", fields.join(" "));
        assert_eq!(parse_start_time(&stat), Some(123456));
        assert_eq!(parse_start_time("4242 (x"), None);
        let ours = start_time(std::process::id()).expect("our own start time");
        assert!(ours > 0);
        assert_eq!(start_time(std::process::id()), Some(ours));
    }
}
