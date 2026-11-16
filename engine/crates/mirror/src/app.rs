//! Finding an application on the desktop, activating its bridge and opening
//! a peer-to-peer connection to it.

use std::collections::HashSet;
use std::time::{Duration, Instant};

use zbus::Connection;

use crate::Error;
use crate::client::Client;
use crate::consts::{
    IFACE_ACCESSIBLE, IFACE_APPLICATION, IFACE_PROPERTIES, REGISTRY_NAME, ROLE_DOCUMENT_WEB,
    ROOT_PATH,
};
use crate::decode::{self, ObjRef};
use crate::discovery::Peer;
use crate::time;

pub async fn desktop_apps(bus: &Connection) -> Result<Vec<ObjRef>, Error> {
    let reg = Client::bus(bus.clone(), REGISTRY_NAME);
    let msg = reg
        .request(ROOT_PATH, IFACE_ACCESSIBLE, "GetChildren", &())
        .await?;
    reg.decode(
        &msg,
        ROOT_PATH,
        IFACE_ACCESSIBLE,
        "GetChildren",
        "a(so)",
        decode::children,
    )
}

pub async fn property_string(
    client: &Client,
    path: &str,
    iface: &str,
    name: &str,
) -> Result<String, Error> {
    let msg = client
        .request(path, IFACE_PROPERTIES, "Get", &(iface, name))
        .await?;
    client.decode(
        &msg,
        path,
        IFACE_PROPERTIES,
        "Get",
        "v holding s",
        decode::string_property,
    )
}

/// The unique bus name of the first registered application whose accessible
/// name contains `want`, ignoring case. Polls until `timeout`.
pub async fn find_app(bus: &Connection, want: &str, timeout: Duration) -> Result<String, Error> {
    let end = Instant::now() + timeout;
    let want = want.to_lowercase();
    loop {
        for app in desktop_apps(bus).await.unwrap_or_default() {
            let c = Client::bus(bus.clone(), &app.bus);
            if let Ok(name) = property_string(&c, &app.path, IFACE_ACCESSIBLE, "Name").await
                && name.to_lowercase().contains(&want)
            {
                return Ok(app.bus);
            }
        }
        if Instant::now() >= end {
            return Err(Error::NotFound(format!(
                "no application named like {want:?}"
            )));
        }
        time::sleep(Duration::from_millis(250))?.await;
    }
}

/// `GetApplicationBusAddress` sent over the bus. Besides returning the
/// peer-to-peer address, this is what activates the application's bridge: it
/// builds the bridge cache and registers `/org/a11y/atspi/cache`. Returns an
/// empty string when the application offers no peer-to-peer server. `bus`
/// must be a bus client: the same call over a peer-to-peer connection would
/// need one to exist already.
pub async fn activate(bus: &Client) -> Result<String, Error> {
    let member = "GetApplicationBusAddress";
    let msg = bus
        .request(ROOT_PATH, IFACE_APPLICATION, member, &())
        .await?;
    bus.decode(
        &msg,
        ROOT_PATH,
        IFACE_APPLICATION,
        member,
        "s",
        decode::string,
    )
}

/// Retries `GetItems` while it answers UnknownMethod, which means activation
/// has not finished, and returns the first real answer.
pub async fn get_items_once_active(
    client: &Client,
    timeout: Duration,
) -> Result<Vec<decode::CacheItem>, Error> {
    let end = Instant::now() + timeout;
    loop {
        match crate::fetch::get_items(client).await {
            Err(Error::Call(c)) if c.kind.is_unknown_method() && Instant::now() < end => {
                time::sleep(Duration::from_millis(100))?.await;
            }
            r => return r,
        }
    }
}

/// The process and user that own `name` on the bus, as the bus daemon saw
/// them when that connection was made.
pub async fn owner(bus: &Connection, name: &str) -> Result<Peer, Error> {
    let ask = |member: &'static str| async move {
        let msg = bus
            .call_method(
                Some("org.freedesktop.DBus"),
                "/org/freedesktop/DBus",
                Some("org.freedesktop.DBus"),
                member,
                &name,
            )
            .await?;
        Ok::<u32, Error>(msg.body().deserialize()?)
    };
    Ok(Peer {
        pid: ask("GetConnectionUnixProcessID").await?,
        uid: ask("GetConnectionUnixUser").await?,
    })
}

/// Registers an event class with the registry, as a client that wants
/// events must, so the registry and every bridge count it as a listener.
pub async fn register_event(bus: &Connection, event: &str) -> Result<(), Error> {
    let reg = Client::bus(bus.clone(), REGISTRY_NAME);
    reg.request(
        crate::consts::REGISTRY_PATH,
        "org.a11y.atspi.Registry",
        "RegisterEvent",
        &(event, Vec::<&str>::new()),
    )
    .await?;
    Ok(())
}

/// How many objects [`find_document`] looks at before it gives up. An
/// application's own windows and toolbars come to a few thousand at most,
/// and documents are not descended into.
pub const FIND_DOCUMENT_MAX_OBJECTS: usize = 100_000;

/// Depth first from the application root for a web document whose name
/// contains `title`, without descending into other documents. Each object is
/// looked at once, however the application lists it.
pub async fn find_document(client: &Client, title: &str) -> Result<Option<String>, Error> {
    let mut stack = vec![ROOT_PATH.to_owned()];
    let mut seen = HashSet::new();
    while let Some(p) = stack.pop() {
        if !seen.insert(p.clone()) {
            continue;
        }
        if seen.len() > FIND_DOCUMENT_MAX_OBJECTS {
            return Err(Error::NotFound(format!(
                "no document titled {title:?} among the first {FIND_DOCUMENT_MAX_OBJECTS} objects"
            )));
        }
        let Ok(msg) = client.call(&p, IFACE_ACCESSIBLE, "GetRole", &()).await else {
            continue;
        };
        let role = client.decode(&msg, &p, IFACE_ACCESSIBLE, "GetRole", "u", decode::role)?;
        if role == ROLE_DOCUMENT_WEB {
            let name = property_string(client, &p, IFACE_ACCESSIBLE, "Name")
                .await
                .unwrap_or_default();
            if name.contains(title) {
                return Ok(Some(p));
            }
            continue;
        }
        let Ok(msg) = client.call(&p, IFACE_ACCESSIBLE, "GetChildren", &()).await else {
            continue;
        };
        let kids = client.decode(
            &msg,
            &p,
            IFACE_ACCESSIBLE,
            "GetChildren",
            "a(so)",
            decode::children,
        )?;
        stack.extend(
            kids.into_iter()
                .rev()
                .filter(|k| !k.is_null() && k.bus == client.app_bus && !seen.contains(&k.path))
                .map(|k| k.path),
        );
    }
    Ok(None)
}
