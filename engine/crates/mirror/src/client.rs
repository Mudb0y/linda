//! One connection to one application, with the counters a benchmark needs.

use std::cell::Cell;

use serde::Serialize;
use zbus::{Connection, DynamicType, Message};

use crate::Error;

#[derive(Debug, Default, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct Counters {
    pub calls: u64,
    pub reply_bytes: u64,
    pub errors: u64,
    pub timeouts: u64,
}

impl std::ops::Sub for Counters {
    type Output = Counters;
    fn sub(self, o: Counters) -> Counters {
        Counters {
            calls: self.calls - o.calls,
            reply_bytes: self.reply_bytes - o.reply_bytes,
            errors: self.errors - o.errors,
            timeouts: self.timeouts - o.timeouts,
        }
    }
}

impl std::ops::Add for Counters {
    type Output = Counters;
    fn add(self, o: Counters) -> Counters {
        Counters {
            calls: self.calls + o.calls,
            reply_bytes: self.reply_bytes + o.reply_bytes,
            errors: self.errors + o.errors,
            timeouts: self.timeouts + o.timeouts,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Transport {
    Bus,
    PeerToPeer,
}

/// Calls go to `dest` on the bus, or with no destination on a peer-to-peer
/// connection, where the peer is the application itself. Object references in
/// replies still carry the application's unique bus name in both cases, so
/// `app_bus` is kept to recognise references into the same application.
pub struct Client {
    pub conn: Connection,
    dest: Option<String>,
    pub app_bus: String,
    pub transport: Transport,
    counters: Cell<Counters>,
}

impl Client {
    pub fn bus(conn: Connection, app_bus: &str) -> Self {
        Self {
            conn,
            dest: Some(app_bus.to_owned()),
            app_bus: app_bus.to_owned(),
            transport: Transport::Bus,
            counters: Cell::default(),
        }
    }

    pub fn p2p(conn: Connection, app_bus: &str) -> Self {
        Self {
            conn,
            dest: None,
            app_bus: app_bus.to_owned(),
            transport: Transport::PeerToPeer,
            counters: Cell::default(),
        }
    }

    pub fn counters(&self) -> Counters {
        self.counters.get()
    }

    /// A failure of a call this client made, with where it happened.
    pub fn failure(&self, path: &str, iface: &str, member: &str, kind: FailureKind) -> CallFailure {
        CallFailure {
            app: self.app_bus.clone(),
            path: path.to_owned(),
            interface: iface.to_owned(),
            member: member.to_owned(),
            kind,
        }
    }

    /// [`Client::call`], with a failure saying which application, object and
    /// method it was.
    pub async fn request<B>(
        &self,
        path: &str,
        iface: &str,
        member: &str,
        body: &B,
    ) -> Result<Message, Error>
    where
        B: serde::Serialize + DynamicType,
    {
        self.call(path, iface, member, body).await.map_err(|e| {
            self.failure(path, iface, member, FailureKind::of(&e))
                .into()
        })
    }

    /// Decodes a reply of [`Client::request`], with a reply of the wrong
    /// shape reported as such.
    pub fn decode<T>(
        &self,
        msg: &Message,
        path: &str,
        iface: &str,
        member: &str,
        expected: &str,
        decode: impl FnOnce(&Message) -> zbus::Result<T>,
    ) -> Result<T, Error> {
        decode(msg).map_err(|_| {
            self.failure(path, iface, member, FailureKind::undecodable(msg, expected))
                .into()
        })
    }

    pub async fn call<B>(
        &self,
        path: &str,
        iface: &str,
        member: &str,
        body: &B,
    ) -> zbus::Result<Message>
    where
        B: serde::Serialize + DynamicType,
    {
        let mut c = self.counters.get();
        c.calls += 1;
        self.counters.set(c);
        let r = self
            .conn
            .call_method(self.dest.as_deref(), path, Some(iface), member, body)
            .await;
        let mut c = self.counters.get();
        match &r {
            Ok(m) => c.reply_bytes += m.data().len() as u64,
            Err(e) => {
                c.errors += 1;
                if is_timeout(e) {
                    c.timeouts += 1;
                }
            }
        }
        self.counters.set(c);
        r
    }
}

pub fn is_timeout(e: &zbus::Error) -> bool {
    matches!(e, zbus::Error::InputOutput(io) if io.kind() == std::io::ErrorKind::TimedOut)
}

const UNKNOWN_METHOD: &str = "org.freedesktop.DBus.Error.UnknownMethod";
const UNKNOWN_OBJECT: &str = "org.freedesktop.DBus.Error.UnknownObject";

/// UnknownMethod from a bridge object that should exist means the bridge has
/// not been activated yet, not that the interface is missing.
pub fn is_unknown_method(e: &zbus::Error) -> bool {
    error_name(e).is_some_and(|n| n == UNKNOWN_METHOD)
}

/// Whether the application said the object does not exist, which the bridge
/// answers for a path it no longer holds (`droute_object_does_not_exist_error`
/// in at-spi2-core). Any other failure, a timeout above all, says nothing
/// about whether the object is still there.
pub fn is_gone(e: &zbus::Error) -> bool {
    error_name(e).is_some_and(|n| n == UNKNOWN_OBJECT)
}

fn error_name(e: &zbus::Error) -> Option<String> {
    use zbus::DBusError;
    match e {
        zbus::Error::FDO(f) => Some(f.name().to_string()),
        zbus::Error::MethodError(name, _, _) => Some(name.to_string()),
        _ => None,
    }
}

/// What one failed call was, so that the application that misbehaved, and
/// how, can be named in a log or a bug report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CallFailure {
    /// The application's unique bus name.
    pub app: String,
    pub path: String,
    pub interface: String,
    pub member: String,
    pub kind: FailureKind,
}

impl std::fmt::Display for CallFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}.{} on {} of {}: {}",
            self.interface, self.member, self.path, self.app, self.kind
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "kebab-case")]
pub enum FailureKind {
    /// No reply within the method timeout.
    Timeout,
    /// The application answered with an error.
    ErrorReply {
        name: String,
        message: Option<String>,
    },
    /// A reply that does not have the shape the method's reply has.
    Undecodable { expected: String, received: String },
    /// The call could not be sent, or the connection broke.
    Connection { message: String },
    /// The call was never made, for the reason given.
    NotMade { reason: &'static str },
    /// The reply was not taken in, because it would have taken a walk past
    /// its object limit.
    OverLimit,
}

impl FailureKind {
    pub fn of(e: &zbus::Error) -> Self {
        if is_timeout(e) {
            return FailureKind::Timeout;
        }
        match e {
            zbus::Error::FDO(f) => {
                use zbus::DBusError;
                FailureKind::ErrorReply {
                    name: f.name().to_string(),
                    message: f.description().map(str::to_owned),
                }
            }
            zbus::Error::MethodError(name, message, _) => FailureKind::ErrorReply {
                name: name.to_string(),
                message: message.clone(),
            },
            _ => FailureKind::Connection {
                message: e.to_string(),
            },
        }
    }

    pub fn undecodable(msg: &Message, expected: &str) -> Self {
        FailureKind::Undecodable {
            expected: expected.to_owned(),
            received: msg.body().signature().to_string(),
        }
    }

    /// Whether the application said the object does not exist; see
    /// [`is_gone`].
    pub fn is_gone(&self) -> bool {
        matches!(self, FailureKind::ErrorReply { name, .. } if name == UNKNOWN_OBJECT)
    }

    pub fn is_unknown_method(&self) -> bool {
        matches!(self, FailureKind::ErrorReply { name, .. } if name == UNKNOWN_METHOD)
    }

    /// Whether asking again later can give a different answer. An error
    /// reply or a reply of the wrong shape is the application's answer, and a
    /// subtree too big for the limit stays too big; a call that timed out or
    /// was never made had no answer.
    pub fn worth_retrying(&self) -> bool {
        matches!(
            self,
            FailureKind::Timeout | FailureKind::Connection { .. } | FailureKind::NotMade { .. }
        )
    }
}

impl std::fmt::Display for FailureKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FailureKind::Timeout => f.write_str("no reply in time"),
            FailureKind::ErrorReply { name, message } => match message {
                Some(m) => write!(f, "{name}: {m}"),
                None => f.write_str(name),
            },
            FailureKind::Undecodable { expected, received } => {
                write!(f, "reply of signature {received}, expected {expected}")
            }
            FailureKind::Connection { message } => f.write_str(message),
            FailureKind::NotMade { reason } => write!(f, "not made: {reason}"),
            FailureKind::OverLimit => f.write_str("not taken: over the object limit"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn error_reply(name: &str) -> zbus::Error {
        let call = Message::method_call("/x", "GetChildren")
            .build(&())
            .unwrap();
        let reply = Message::error(&call.header(), name)
            .build(&("detail",))
            .unwrap();
        zbus::Error::from(reply)
    }

    #[test]
    fn only_unknown_object_means_gone() {
        assert!(is_gone(&error_reply(UNKNOWN_OBJECT)));
        assert!(FailureKind::of(&error_reply(UNKNOWN_OBJECT)).is_gone());
        let timeout = zbus::Error::from(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "timed out",
        ));
        for e in [
            error_reply(UNKNOWN_METHOD),
            error_reply("org.freedesktop.DBus.Error.Failed"),
            error_reply("org.example.Custom"),
            timeout,
        ] {
            assert!(!is_gone(&e), "{e}");
            assert!(!FailureKind::of(&e).is_gone(), "{e}");
        }
        assert!(is_unknown_method(&error_reply(UNKNOWN_METHOD)));
        assert!(!is_unknown_method(&error_reply(UNKNOWN_OBJECT)));
    }

    #[test]
    fn failures_name_the_error_and_where() {
        let f = CallFailure {
            app: ":1.7".into(),
            path: "/o".into(),
            interface: "org.a11y.atspi.Accessible".into(),
            member: "GetChildren".into(),
            kind: FailureKind::of(&error_reply("org.example.Custom")),
        };
        assert_eq!(
            f.kind,
            FailureKind::ErrorReply {
                name: "org.example.Custom".into(),
                message: Some("detail".into())
            }
        );
        assert_eq!(
            f.to_string(),
            "org.a11y.atspi.Accessible.GetChildren on /o of :1.7: org.example.Custom: detail"
        );
        let timeout = zbus::Error::from(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "timed out",
        ));
        assert_eq!(FailureKind::of(&timeout), FailureKind::Timeout);
        assert!(FailureKind::Timeout.worth_retrying());
        assert!(
            !FailureKind::of(&error_reply("org.freedesktop.DBus.Error.Failed")).worth_retrying()
        );
        let msg = Message::method_call("/x", "Reply").build(&7u32).unwrap();
        assert_eq!(
            FailureKind::undecodable(&msg, "a(so)"),
            FailureKind::Undecodable {
                expected: "a(so)".into(),
                received: "u".into()
            }
        );
    }
}
