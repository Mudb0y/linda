//! Who may be the client, and which session is in front: logind and polkit on the system bus,
//! behind a trait so that the decision can be tested without them. Both run on threads of their
//! own, so that a slow or hung service never holds up a key.

use std::collections::HashMap;
use std::future::Future;
use std::io;
use std::num::NonZeroUsize;
use std::thread;
use std::time::Duration;

use futures_util::StreamExt;
use futures_util::future::{Either, select};
use zbus::message::Type;
use zbus::{Connection, MatchRule, MessageStream, OwnedObjectPath, OwnedVariant, Variant};

use crate::core::{Front, Peer, Refusal};
use crate::protocol::{DBUS_CALL_TIMEOUT, DBUS_RETRY, Goodbye, POLKIT_ACTION, SESSION_RECHECK};

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
/// taken as belonging to its user's graphical session, as polkit itself takes it. The process's
/// start time is read before and after, so that a process that ended meanwhile, whose number
/// another may have taken, is not admitted in its place. Returns the session's ID.
pub fn admit(
    login: &mut dyn Login,
    peer: &Peer,
    start_time: &dyn Fn(u32) -> Option<u64>,
) -> Result<String, Refusal> {
    let not_in_front = |why: String| (Goodbye::NotInFront, why);
    let no_answer = |why: String| (Goodbye::NoAnswer, why);
    let Some(started) = start_time(peer.pid) else {
        return Err(not_in_front(format!("process {} has ended", peer.pid)));
    };
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
    if start_time(peer.pid) != Some(started) {
        return Err(not_in_front(format!(
            "process {} ended while it was being admitted",
            peer.pid
        )));
    }
    Ok(s.id)
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
            Ok(SessionInfo {
                id,
                uid,
                seat,
                active,
                remote,
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
    Ok(Front::Session { id, uid })
}

fn error_name(e: &zbus::Error) -> Option<&str> {
    match e {
        zbus::Error::MethodError(name, _, _) => Some(name.as_str()),
        _ => None,
    }
}

/// Starts the thread that follows which session is in front on seat0 and posts each change. While
/// the system bus or logind cannot be reached it posts [`Front::Unknown`], which makes the helper
/// let go, and tries again every DBUS_RETRY.
pub fn watch_front(post: impl Fn(Front) + Send + 'static) -> io::Result<thread::JoinHandle<()>> {
    thread::Builder::new().name("logind".into()).spawn(move || {
        loop {
            let mut last = None;
            let e = zbus::block_on(follow(&post, &mut last));
            log!("lost track of the session in front: {e}");
            post(Front::Unknown);
            thread::sleep(DBUS_RETRY);
        }
    })
}

async fn follow(post: &dyn Fn(Front), last: &mut Option<Front>) -> zbus::Error {
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
            let now = within(DBUS_CALL_TIMEOUT, front(&conn)).await??;
            if last.as_ref() != Some(&now) {
                post(now.clone());
                *last = Some(now);
            }
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
                    },
                )]),
                front: Front::Session {
                    id: "2".into(),
                    uid: 1000,
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
        assert_eq!(admit(&mut f, &PEER, &started), Ok("2".into()));
        assert!(f.asked_polkit);
    }

    #[test]
    fn a_process_outside_any_session_counts_as_its_users_graphical_one() {
        let mut f = Fake::usual();
        f.display = f.by_pid.clone().unwrap();
        f.by_pid = Ok(None);
        assert_eq!(admit(&mut f, &PEER, &started), Ok("2".into()));
        f.display = None;
        let e = admit(&mut f, &PEER, &started).unwrap_err();
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
            ("inactive", Box::new(|f| f.edit(|s| s.active = false))),
            (
                "not in front",
                Box::new(|f| {
                    f.front = Front::Session {
                        id: "3".into(),
                        uid: 1000,
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
            let e = admit(&mut f, &PEER, &started).unwrap_err();
            assert_eq!(e.0, Goodbye::NotInFront, "{what}: {}", e.1);
            assert!(!f.asked_polkit, "{what}");
        }
    }

    #[test]
    fn polkit_has_the_last_word() {
        let mut f = Fake::usual();
        f.allowed = Ok(false);
        assert_eq!(
            admit(&mut f, &PEER, &started).unwrap_err().0,
            Goodbye::NotAuthorized
        );
        f.allowed = Err("polkit is not running".into());
        assert_eq!(
            admit(&mut f, &PEER, &started).unwrap_err().0,
            Goodbye::NoAnswer
        );
    }

    #[test]
    fn a_failing_logind_is_no_answer() {
        let mut f = Fake::usual();
        f.by_pid = Err("timed out".into());
        let e = admit(&mut f, &PEER, &started).unwrap_err();
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
        let e = admit(&mut f, &PEER, &changing).unwrap_err();
        assert!(e.1.contains("ended while"), "{}", e.1);
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
