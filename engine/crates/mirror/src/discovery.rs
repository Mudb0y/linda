//! Finding the accessibility bus in libatspi's order (`atspi_get_a11y_bus`,
//! `atspi/atspi-misc.c`), so the engine lands on the same bus as Orca does,
//! and connecting to it and to applications' own servers.
//!
//! Addresses come from places others control: the environment, a root window
//! property any X client can set, and above all an application's answer to
//! `GetApplicationBusAddress`. zbus would follow a `tcp:` address anywhere,
//! and for `nonce-tcp:` it reads the whole of a named local file and sends it
//! to the peer. So only a unix socket, by path or abstract name, is accepted,
//! and an application's own server must belong to the process that owns the
//! application's bus name, since anyone can bind an abstract name.

use std::os::unix::net::SocketAddr;
use std::time::Duration;

use serde::Serialize;
use zbus::Connection;
use zbus::address::transport::{Transport, UnixSocket};

use crate::x11::{self, Display};
use crate::{Error, time};

/// How long to wait for what. The two are kept apart because a busy
/// application services its peer-to-peer handshake on its main loop, which
/// can take longer than one call should.
#[derive(Debug, Clone, Copy)]
pub struct Timeouts {
    /// For each method call.
    pub method: Duration,
    /// For connecting and the authentication handshake.
    pub connect: Duration,
}

/// The process at the other end of a peer-to-peer socket must be this one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Peer {
    pub pid: u32,
    pub uid: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Source {
    Environment,
    X11RootProperty,
    SessionBus,
}

#[derive(Debug, Clone, Serialize)]
pub struct BusAddress {
    pub address: String,
    pub source: Source,
    /// Earlier routes that failed or named an address of another kind,
    /// which were passed over.
    pub refused: Vec<String>,
}

/// The address as zbus parses it, when it names a unix socket by path or
/// by abstract name.
fn unix_address(address: &str) -> Result<(zbus::Address, SocketAddr), Error> {
    let refuse = || Error::Refused(format!("{address:?} is not a unix socket path or name"));
    let parsed: zbus::Address = address.parse().map_err(|_| refuse())?;
    let socket = match parsed.transport() {
        Transport::Unix(u) => match u.path() {
            UnixSocket::File(p) => SocketAddr::from_pathname(p).map_err(|_| refuse())?,
            UnixSocket::Abstract(name) => {
                use std::os::linux::net::SocketAddrExt;
                use std::os::unix::ffi::OsStrExt;
                SocketAddr::from_abstract_name(name.as_bytes()).map_err(|_| refuse())?
            }
            _ => return Err(refuse()),
        },
        _ => return Err(refuse()),
    };
    Ok((parsed, socket))
}

/// Returns the accessibility bus address and where it was found. The session
/// bus is only contacted when the first two routes find nothing usable.
pub async fn find_bus_address(timeouts: Timeouts) -> Result<BusAddress, Error> {
    let mut refused = Vec::new();
    let mut usable = |address: String, source: Source| match unix_address(&address) {
        Ok(_) => Some(BusAddress {
            address,
            source,
            refused: Vec::new(),
        }),
        Err(e) => {
            refused.push(format!("{source:?}: {e}"));
            None
        }
    };
    let mut found = std::env::var("AT_SPI_BUS_ADDRESS")
        .ok()
        .filter(|a| !a.is_empty())
        .and_then(|a| usable(a, Source::Environment));
    let display = std::env::var("DISPLAY").ok().filter(|d| !d.is_empty());
    let wayland = std::env::var_os("WAYLAND_DISPLAY").is_some_and(|d| !d.is_empty());
    if found.is_none()
        && !wayland
        && let Some(display) = display
    {
        found = match x11_root_property(&display, timeouts).await {
            Ok(a) => a.and_then(|a| usable(a, Source::X11RootProperty)),
            Err(e) => {
                refused.push(format!("{:?}: {e}", Source::X11RootProperty));
                None
            }
        };
    }
    let mut found = match found {
        Some(f) => f,
        None => {
            let session = time::within(
                timeouts.connect,
                "connecting to the session bus",
                zbus::connection::Builder::session()
                    .method_timeout(timeouts.method)
                    .build(),
            )
            .await??;
            let reply = session
                .call_method(
                    Some("org.a11y.Bus"),
                    "/org/a11y/bus",
                    Some("org.a11y.Bus"),
                    "GetAddress",
                    &(),
                )
                .await?;
            let address: String = reply.body().deserialize()?;
            unix_address(&address)?;
            BusAddress {
                address,
                source: Source::SessionBus,
                refused: Vec::new(),
            }
        }
    };
    found.refused = refused;
    Ok(found)
}

/// The `AT_SPI_BUS` string on the root window, which the bus launcher sets on
/// X11, bounded by the connect timeout. A failure means "not advertised
/// here", and the caller falls back to the session bus.
async fn x11_root_property(display: &str, timeouts: Timeouts) -> Result<Option<String>, Error> {
    let display = Display::parse(display)
        .ok_or_else(|| Error::Refused(format!("DISPLAY {display:?} is not a local display")))?;
    time::within(
        timeouts.connect,
        "reading the X server's root window",
        x11::root_property(display, "AT_SPI_BUS"),
    )
    .await?
}

/// Opens a bus connection to the accessibility bus. Connecting and the
/// handshake are bounded by the connect timeout and every call by the method
/// timeout, so that a hung bus or application can never block for ever.
pub async fn connect_bus(address: &str, timeouts: Timeouts) -> Result<Connection, Error> {
    let (parsed, _) = unix_address(address)?;
    let build = zbus::connection::Builder::address(parsed)
        .method_timeout(timeouts.method)
        .build();
    Ok(time::within(timeouts.connect, &format!("connecting to {address}"), build).await??)
}

/// Opens a peer-to-peer connection to an application's own server; no Hello,
/// no bus daemon in between. The server must be a unix socket held by
/// `peer`, the process that owns the application's name on the bus; anything
/// else is refused before a byte is sent.
pub async fn connect_p2p(
    address: &str,
    peer: Peer,
    timeouts: Timeouts,
) -> Result<Connection, Error> {
    let (_, socket) = unix_address(address)?;
    let connect = async {
        let runtime = zruntime::SharedRuntime::current()?;
        let stream = zruntime::net::unix::UnixStream::connect_addr(&runtime, &socket)
            .await?
            .into_std();
        let cred = rustix::net::sockopt::socket_peercred(&stream).map_err(std::io::Error::from)?;
        let found = Peer {
            pid: u32::try_from(cred.pid.as_raw_pid()).unwrap_or(0),
            uid: cred.uid.as_raw(),
        };
        if found != peer {
            return Err(Error::Refused(format!(
                "{address:?} is served by process {} of user {}, not by the application's \
                 process {} of user {}",
                found.pid, found.uid, peer.pid, peer.uid
            )));
        }
        Ok(zbus::connection::Builder::unix_stream(stream)
            .p2p()
            .method_timeout(timeouts.method)
            .build()
            .await?)
    };
    time::within(
        timeouts.connect,
        &format!("connecting to {address}"),
        connect,
    )
    .await?
}

#[cfg(test)]
mod tests {
    use std::os::linux::net::SocketAddrExt;
    use std::os::unix::net::UnixListener;
    use std::time::Instant;

    use super::*;
    use crate::testing::{returns, rig};

    #[test]
    fn an_x_server_that_never_answers_is_given_up_on_in_time() {
        rig();
        // The rig's own network namespace holds this name, and no server.
        let name = SocketAddr::from_abstract_name(b"/tmp/.X11-unix/X42").unwrap();
        let listener = UnixListener::bind_addr(&name).unwrap();
        let timeouts = Timeouts {
            method: Duration::from_secs(30),
            connect: Duration::from_millis(300),
        };
        let t = Instant::now();
        let r = returns(move || {
            let _held = listener;
            zbus::block_on(x11_root_property(":42", timeouts)).map(|_| ())
        });
        assert!(matches!(r, Err(Error::TimedOut(_))), "{r:?}");
        assert!(t.elapsed() < Duration::from_secs(3), "{:?}", t.elapsed());
        let r = zbus::block_on(x11_root_property("elsewhere:0", timeouts));
        assert!(matches!(r, Err(Error::Refused(_))), "{r:?}");
    }

    #[test]
    fn only_unix_sockets_are_accepted() {
        for ok in [
            "unix:path=/run/user/1000/at-spi/bus_0",
            "unix:path=/tmp/dbus-x%2cy,guid=0123456789abcdef0123456789abcdef",
            "unix:abstract=/tmp/dbus-ABC",
        ] {
            assert!(unix_address(ok).is_ok(), "{ok}");
        }
        for refused in [
            "tcp:host=127.0.0.1,port=4000",
            "nonce-tcp:host=127.0.0.1,port=4000,noncefile=/etc/passwd",
            "unix:dir=/tmp",
            "unix:tmpdir=/tmp",
            "unix:runtime=yes",
            "unixexec:path=/bin/sh",
            "autolaunch:",
            "",
            "nonsense",
        ] {
            assert!(
                matches!(unix_address(refused), Err(Error::Refused(_))),
                "{refused}"
            );
        }
    }
}
