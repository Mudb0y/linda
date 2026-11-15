//! Finding an application on the desktop, activating its bridge and opening
//! a peer-to-peer connection to it.

use std::time::{Duration, Instant};

use zbus::Connection;

use crate::Error;
use crate::client::{Client, is_unknown_method};
use crate::consts::{
    IFACE_ACCESSIBLE, IFACE_APPLICATION, IFACE_PROPERTIES, REGISTRY_NAME, ROLE_DOCUMENT_WEB,
    ROOT_PATH,
};
use crate::decode::{self, ObjRef};

pub async fn desktop_apps(bus: &Connection) -> Result<Vec<ObjRef>, Error> {
    let reg = Client::bus(bus.clone(), REGISTRY_NAME);
    let msg = reg
        .call(ROOT_PATH, IFACE_ACCESSIBLE, "GetChildren", &())
        .await?;
    Ok(decode::children(&msg)?)
}

pub async fn property_string(
    client: &Client,
    path: &str,
    iface: &str,
    name: &str,
) -> Result<String, Error> {
    let msg = client
        .call(path, IFACE_PROPERTIES, "Get", &(iface, name))
        .await?;
    Ok(decode::string_property(&msg)?)
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
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// `GetApplicationBusAddress` sent over the bus. Besides returning the
/// peer-to-peer address, this is what activates the application's bridge: it
/// builds the bridge cache and registers `/org/a11y/atspi/cache`. Returns an
/// empty string when the application offers no peer-to-peer server. `bus`
/// must be a bus client: the same call over a peer-to-peer connection would
/// need one to exist already.
pub async fn activate(bus: &Client) -> Result<String, Error> {
    let msg = bus
        .call(
            ROOT_PATH,
            IFACE_APPLICATION,
            "GetApplicationBusAddress",
            &(),
        )
        .await?;
    Ok(decode::string(&msg)?)
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
            Err(e) if is_unknown_method(&e) && Instant::now() < end => {
                std::thread::sleep(Duration::from_millis(100));
            }
            r => return Ok(r?),
        }
    }
}

/// The process ID of the connection that owns `name` on the bus.
pub async fn owner_pid(bus: &Connection, name: &str) -> Result<u32, Error> {
    let msg = bus
        .call_method(
            Some("org.freedesktop.DBus"),
            "/org/freedesktop/DBus",
            Some("org.freedesktop.DBus"),
            "GetConnectionUnixProcessID",
            &name,
        )
        .await?;
    Ok(msg.body().deserialize()?)
}

/// Registers an event class with the registry, as a client that wants
/// events must, so the registry and every bridge count it as a listener.
pub async fn register_event(bus: &Connection, event: &str) -> Result<(), Error> {
    let reg = Client::bus(bus.clone(), REGISTRY_NAME);
    reg.call(
        crate::consts::REGISTRY_PATH,
        "org.a11y.atspi.Registry",
        "RegisterEvent",
        &(event, Vec::<&str>::new()),
    )
    .await?;
    Ok(())
}

/// Depth first from the application root for a web document whose name
/// contains `title`, without descending into other documents.
pub async fn find_document(client: &Client, title: &str) -> Result<Option<String>, Error> {
    let mut stack = vec![ROOT_PATH.to_owned()];
    while let Some(p) = stack.pop() {
        let Ok(msg) = client.call(&p, IFACE_ACCESSIBLE, "GetRole", &()).await else {
            continue;
        };
        if decode::role(&msg)? == ROLE_DOCUMENT_WEB {
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
        let kids = decode::children(&msg)?;
        stack.extend(
            kids.into_iter()
                .rev()
                .filter(|k| !k.is_null() && k.bus == client.app_bus)
                .map(|k| k.path),
        );
    }
    Ok(None)
}
