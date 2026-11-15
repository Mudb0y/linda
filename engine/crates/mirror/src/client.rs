//! One connection to one application, with the counters a benchmark needs.

use std::cell::Cell;

use serde::Serialize;
use zbus::{Connection, DynamicType, Message};

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

/// UnknownMethod from a bridge object that should exist means the bridge has
/// not been activated yet, not that the interface is missing.
pub fn is_unknown_method(e: &zbus::Error) -> bool {
    match e {
        zbus::Error::FDO(f) => matches!(**f, zbus::fdo::Error::UnknownMethod(_)),
        zbus::Error::MethodError(name, _, _) => {
            name.as_str() == "org.freedesktop.DBus.Error.UnknownMethod"
        }
        _ => false,
    }
}
