//! The engine against fake applications on the rig's accessibility bus, each offering a server of
//! its own that cannot be used: a socket that is not there, one nobody listens on, or one that
//! closes in the middle of a render. Each document is rendered over the bus instead, the log is told
//! once, and the application is read over the bus from then on. Like everything that speaks D-Bus
//! these run only inside tests/linux/rig, where cargo test starts them.

use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use futures_util::StreamExt;
use mirror::consts::ROOT_PATH;
use mirror::discovery::{self, Timeouts};
use serde_json::Value;
use zbus::message::Type;
use zbus::{Connection, Message, MessageStream, ObjectPath, Variant};

const DOC: &str = "/doc";
const PARAGRAPH: &str = "/doc/p";
const TEXT: &str = "Read over the bus.";
const ROLE_DOCUMENT_WEB: u32 = 95;
const ROLE_PARAGRAPH: u32 = 73;

fn rig() {
    let inside = std::env::var("LINDA_RIG").as_deref() == Ok("1")
        && std::fs::read_to_string("/proc/1/comm").is_ok_and(|c| c.trim_end() == "bwrap")
        && !std::path::Path::new("/run/user").exists();
    assert!(
        inside,
        "speaks D-Bus, so runs only inside tests/linux/rig, as cargo test does"
    );
}

fn timeouts() -> Timeouts {
    Timeouts {
        method: Duration::from_secs(5),
        connect: Duration::from_secs(5),
    }
}

fn reply<B: zbus::export::serde::Serialize + zbus::DynamicType>(
    call: &Message,
    body: &B,
) -> Message {
    Message::method_return(&call.header()).build(body).unwrap()
}

/// A document of one paragraph, answered the same over the bus and over the application's own
/// server.
fn answer(call: &Message, me: &str, address: &str) -> Message {
    let header = call.header();
    let path = header.path().map(|p| p.to_string()).unwrap_or_default();
    let member = header.member().map(|m| m.to_string()).unwrap_or_default();
    let iface = header
        .interface()
        .map(|i| i.to_string())
        .unwrap_or_default();
    match (member.as_str(), path.as_str()) {
        ("GetApplicationBusAddress", _) => reply(call, &address),
        ("GetRole", DOC) => reply(call, &ROLE_DOCUMENT_WEB),
        ("GetRole", _) => reply(call, &ROLE_PARAGRAPH),
        ("GetState", _) => reply(call, &vec![0u32, 0]),
        ("GetChildren", DOC) => reply(call, &vec![(me, ObjectPath::try_from(PARAGRAPH).unwrap())]),
        ("GetChildren", _) => reply(call, &Vec::<(&str, ObjectPath<'_>)>::new()),
        ("GetInterfaces", PARAGRAPH) => reply(
            call,
            &vec!["org.a11y.atspi.Accessible", "org.a11y.atspi.Text"],
        ),
        ("GetInterfaces", _) => reply(
            call,
            &vec!["org.a11y.atspi.Accessible", "org.a11y.atspi.Document"],
        ),
        ("GetAttributes", _) if iface == "org.a11y.atspi.Accessible" => {
            reply(call, &HashMap::from([("tag", "p")]))
        }
        ("GetText", PARAGRAPH) => reply(call, &TEXT),
        ("Get", _) => {
            let (_, property): (String, String) = call.body().deserialize().unwrap();
            let name = match path.as_str() {
                ROOT_PATH => "Fake application",
                DOC => "Fake document",
                _ => "",
            };
            match property.as_str() {
                "Name" => reply(call, &Variant::new(name)),
                "Description" => reply(call, &Variant::new("")),
                _ => unknown(call),
            }
        }
        _ => unknown(call),
    }
}

fn unknown(call: &Message) -> Message {
    Message::error(&call.header(), "org.freedesktop.DBus.Error.UnknownMethod")
        .build(&("not here",))
        .unwrap()
}

async fn serve(
    conn: Connection,
    me: String,
    address: String,
    calls: Arc<AtomicUsize>,
    close_after: Option<usize>,
) {
    let mut stream = MessageStream::from(&conn);
    while let Some(Ok(call)) = stream.next().await {
        if call.message_type() != Type::MethodCall {
            continue;
        }
        let _ = conn.send(&answer(&call, &me, &address)).await;
        if close_after.is_some_and(|n| calls.fetch_add(1, Ordering::SeqCst) + 1 >= n) {
            break;
        }
    }
    drop(stream);
    let _ = conn.close().await;
}

/// What a fake application's own server does.
enum Server {
    /// A socket path that is not there, as one in a container's own runtime directory.
    Missing,
    /// A socket nobody listens on any more.
    Refused,
    /// A server that answers this many calls and then closes.
    ClosesAfter(usize),
}

struct Fake {
    bus_name: String,
    /// Calls the server answered, and the connections it took.
    p2p_calls: Arc<AtomicUsize>,
    p2p_connections: Arc<AtomicUsize>,
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("transport-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// A fake application on the accessibility bus whose own server does what server says.
fn start(name: &str, server: Server) -> Fake {
    let p2p_calls = Arc::new(AtomicUsize::new(0));
    let p2p_connections = Arc::new(AtomicUsize::new(0));
    let socket = scratch(name).join("socket");
    let address = format!("unix:path={}", socket.display());
    match server {
        Server::Missing => {}
        Server::Refused => drop(UnixListener::bind(&socket).unwrap()),
        Server::ClosesAfter(n) => {
            let listener = UnixListener::bind(&socket).unwrap();
            let (calls, connections) = (p2p_calls.clone(), p2p_connections.clone());
            let address = address.clone();
            std::thread::spawn(move || {
                for stream in listener.incoming() {
                    let Ok(stream) = stream else { return };
                    connections.fetch_add(1, Ordering::SeqCst);
                    let (calls, address) = (calls.clone(), address.clone());
                    std::thread::spawn(move || {
                        zbus::block_on(async move {
                            let conn = zbus::connection::Builder::unix_stream(stream)
                                .server(zbus::Guid::generate())
                                .p2p()
                                .build()
                                .await
                                .unwrap();
                            // Object references carry the application's name on the bus, which this
                            // connection does not know; the bus side tells it below.
                            let me = std::fs::read_to_string(socket_name(&address)).unwrap();
                            serve(conn, me, address, calls, Some(n)).await
                        })
                    });
                }
            });
        }
    }
    let (tx, rx) = std::sync::mpsc::channel();
    let bus_address = address.clone();
    std::thread::spawn(move || {
        zbus::block_on(async move {
            let found = discovery::find_bus_address(timeouts()).await.unwrap();
            let bus = discovery::connect_bus(&found.address, timeouts())
                .await
                .unwrap();
            let me = bus.unique_name().unwrap().to_string();
            std::fs::write(socket_name(&bus_address), &me).unwrap();
            tx.send(me.clone()).unwrap();
            serve(bus, me, bus_address, Arc::new(AtomicUsize::new(0)), None).await
        })
    });
    let bus_name = rx.recv_timeout(Duration::from_secs(10)).unwrap();
    Fake {
        bus_name,
        p2p_calls,
        p2p_connections,
    }
}

fn socket_name(address: &str) -> PathBuf {
    PathBuf::from(format!("{}.name", address.trim_start_matches("unix:path=")))
}

/// The render of the fake document through the engine's own entry point: its text, and its info.
fn render(fake: &Fake) -> (String, Value) {
    let bus = CString::new(fake.bus_name.as_str()).unwrap();
    let doc = CString::new(DOC).unwrap();
    let mut stream = std::ptr::null_mut();
    let mut length = 0usize;
    let mut info = std::ptr::null_mut();
    let ok = unsafe {
        crate::bre_vbuf_render(
            bus.as_ptr(),
            doc.as_ptr(),
            1,
            0,
            &mut stream,
            &mut length,
            &mut info,
        )
    };
    let details: Value =
        serde_json::from_str(&unsafe { CStr::from_ptr(info) }.to_string_lossy()).unwrap();
    let text = if ok == 1 {
        String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(stream, length) })
    } else {
        String::new()
    };
    unsafe {
        crate::bre_vbuf_free_stream(stream, length);
        crate::bre_vbuf_free_str(info);
    }
    assert_eq!(ok, 1, "{details}");
    (text, details)
}

fn assert_read_over_the_bus(fake: &Fake, why: &str) {
    let (text, info) = render(fake);
    assert!(text.contains(TEXT), "{text:?} {info}");
    assert_eq!(info["transport"], "bus", "{info}");
    assert_eq!(info["peerToPeerFailed"], true, "{info}");
    assert_eq!(info["appName"], "Fake application", "{info}");
    let told = info["transportWhy"].as_str().unwrap_or_default();
    assert!(told.contains(why), "{told:?} does not say {why:?}");
    // The next document is read over the bus at once, and the log is not told again.
    let connections = fake.p2p_connections.load(Ordering::SeqCst);
    let (text, info) = render(fake);
    assert!(text.contains(TEXT), "{text:?} {info}");
    assert!(info.get("transport").is_none(), "{info}");
    assert_eq!(info["connectSeconds"], 0.0, "{info}");
    assert_eq!(fake.p2p_connections.load(Ordering::SeqCst), connections);
}

#[test]
fn a_server_of_its_own_that_cannot_be_used_leaves_the_application_on_the_bus() {
    rig();
    let missing = start("missing", Server::Missing);
    assert_read_over_the_bus(&missing, "No such file or directory");
    let refused = start("refused", Server::Refused);
    assert_read_over_the_bus(&refused, "Connection refused");
    // Answered calls enough to start the walk, then gone with calls still waiting.
    let closing = start("closing", Server::ClosesAfter(6));
    assert_read_over_the_bus(&closing, "lost during a render");
    assert_eq!(closing.p2p_connections.load(Ordering::SeqCst), 1);
    assert!(closing.p2p_calls.load(Ordering::SeqCst) >= 6);
    crate::bre_vbuf_shutdown();
}
