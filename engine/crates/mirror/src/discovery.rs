//! Finding the accessibility bus in libatspi's order (`atspi_get_a11y_bus`,
//! `atspi/atspi-misc.c`), so the engine lands on the same bus as Orca does.

use std::time::Duration;

use serde::Serialize;
use x11rb::connection::Connection as _;
use x11rb::protocol::xproto::{AtomEnum, ConnectionExt as _};
use zbus::Connection;

use crate::Error;

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
}

/// Returns the accessibility bus address and where it was found. The session
/// bus is only contacted when the first two routes find nothing.
pub async fn find_bus_address(method_timeout: Duration) -> Result<BusAddress, Error> {
    if let Some(address) = std::env::var("AT_SPI_BUS_ADDRESS")
        .ok()
        .filter(|a| !a.is_empty())
    {
        return Ok(BusAddress {
            address,
            source: Source::Environment,
        });
    }
    let x11 = std::env::var_os("DISPLAY").is_some_and(|d| !d.is_empty())
        && std::env::var_os("WAYLAND_DISPLAY").is_none_or(|d| d.is_empty());
    if x11 && let Some(address) = x11_root_property() {
        return Ok(BusAddress {
            address,
            source: Source::X11RootProperty,
        });
    }
    let session = zbus::connection::Builder::session()
        .method_timeout(method_timeout)
        .build()
        .await?;
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
    Ok(BusAddress {
        address,
        source: Source::SessionBus,
    })
}

/// The `AT_SPI_BUS` string on the root window, which the bus launcher sets on
/// X11. Any failure means "not advertised here", so the caller falls back.
fn x11_root_property() -> Option<String> {
    let (conn, screen) = x11rb::connect(None).ok()?;
    let root = conn.setup().roots.get(screen)?.root;
    let atom = conn
        .intern_atom(true, b"AT_SPI_BUS")
        .ok()?
        .reply()
        .ok()?
        .atom;
    if atom == 0 {
        return None;
    }
    let reply = conn
        .get_property(false, root, atom, AtomEnum::STRING, 0, 4096)
        .ok()?
        .reply()
        .ok()?;
    let value = String::from_utf8(reply.value).ok()?;
    let value = value.trim_end_matches('\0').to_owned();
    (!value.is_empty()).then_some(value)
}

/// Opens a bus connection to the accessibility bus with a method timeout, so
/// that a hung application can never block a call forever.
pub async fn connect_bus(address: &str, method_timeout: Duration) -> Result<Connection, Error> {
    Ok(zbus::connection::Builder::address(address)
        .method_timeout(method_timeout)
        .build()
        .await?)
}

/// Opens a peer-to-peer connection to an application's own server; no Hello,
/// no bus daemon in between.
pub async fn connect_p2p(address: &str, method_timeout: Duration) -> Result<Connection, Error> {
    Ok(zbus::connection::Builder::address(address)
        .p2p()
        .method_timeout(method_timeout)
        .build()
        .await?)
}
