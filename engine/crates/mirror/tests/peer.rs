//! The engine against a fake application on a private socket: one that
//! answers some calls, never answers others, invents objects, or holds a
//! socket without ever speaking. These speak D-Bus, so like everything else
//! that does they run only inside tests/linux/rig, where cargo test starts
//! them.

use std::collections::HashMap;
use std::io::Read;
use std::os::unix::net::{UnixListener, UnixStream};
use std::time::{Duration, Instant};

use futures_util::future::{Either, select};
use futures_util::stream::StreamExt;
use mirror::client::{Client, FailureKind};
use mirror::consts::ROOT_PATH;
use mirror::decode::{AnyData, CacheItem, Event, ObjRef};
use mirror::discovery::{self, Peer, Timeouts};
use mirror::fetch::{self, Field, WalkFields, WalkLimits};
use mirror::maintain::{Mirror, Repair};
use mirror::tree::{Node, Tree};
use mirror::{Error, app, time};
use zbus::message::Type;
use zbus::{Connection, Message, MessageStream, ObjectPath, Variant};

const APP: &str = ":1.7";

const LIMITS: WalkLimits = WalkLimits {
    max_objects: 100_000,
    max_time: Duration::from_secs(60),
};

/// Fails the test anywhere but inside the rig. The variable alone could be
/// copied into another shell; the rig's sandbox is also the first process
/// and has no /run/user.
fn rig() {
    let inside = std::env::var("BASEDREADER_RIG").as_deref() == Ok("1")
        && std::fs::read_to_string("/proc/1/comm").is_ok_and(|c| c.trim_end() == "bwrap")
        && !std::path::Path::new("/run/user").exists();
    assert!(
        inside,
        "speaks D-Bus, so runs only inside tests/linux/rig, as cargo test does"
    );
}

/// What the fake application does with one call: answer it, or let it go
/// unanswered for ever.
type Answer = Box<dyn Fn(&Message) -> Option<Message>>;

fn reply<B: serde::Serialize + zbus::DynamicType>(call: &Message, body: &B) -> Option<Message> {
    Some(Message::method_return(&call.header()).build(body).unwrap())
}

fn error(call: &Message, name: &str) -> Option<Message> {
    Some(
        Message::error(&call.header(), name)
            .build(&("no",))
            .unwrap(),
    )
}

fn children(call: &Message, kids: &[&str]) -> Option<Message> {
    let refs: Vec<(&str, ObjectPath<'_>)> = kids
        .iter()
        .map(|k| (APP, ObjectPath::try_from(*k).unwrap()))
        .collect();
    reply(call, &refs)
}

fn path(call: &Message) -> String {
    call.header()
        .path()
        .map(|p| p.to_string())
        .unwrap_or_default()
}

fn member(call: &Message) -> String {
    call.header()
        .member()
        .map(|m| m.to_string())
        .unwrap_or_default()
}

/// Answers every data call of a walk the same way.
fn data(call: &Message) -> Option<Message> {
    match member(call).as_str() {
        "GetRole" => reply(call, &42u32),
        "GetState" => reply(call, &vec![0u32, 0]),
        "Get" => reply(call, &Variant::new("name")),
        "GetInterfaces" => reply(call, &vec!["org.a11y.atspi.Accessible"]),
        "GetAttributes" => reply(call, &HashMap::<&str, &str>::new()),
        _ => None,
    }
}

async fn serve(conn: Connection, answer: Answer) {
    let mut calls = MessageStream::from(&conn);
    while let Some(Ok(call)) = calls.next().await {
        if call.message_type() == Type::MethodCall
            && let Some(r) = answer(&call)
        {
            let _ = conn.send(&r).await;
        }
    }
}

/// Runs `test` with a client connected to a fake application that answers
/// as `answer` says.
fn against<T>(method_timeout: Duration, answer: Answer, test: impl AsyncFnOnce(&Client) -> T) -> T {
    zbus::block_on(async move {
        let (ours, theirs) = UnixStream::pair().unwrap();
        let server = zbus::connection::Builder::unix_stream(theirs)
            .server(zbus::Guid::generate())
            .p2p()
            .build();
        let client = zbus::connection::Builder::unix_stream(ours)
            .p2p()
            .method_timeout(method_timeout)
            .build();
        let (server, client) = futures_util::future::join(server, client).await;
        let client = Client::p2p(client.unwrap(), APP);
        let serving = std::pin::pin!(serve(server.unwrap(), answer));
        let testing = std::pin::pin!(test(&client));
        match select(testing, serving).await {
            Either::Left((r, _)) => r,
            Either::Right(_) => panic!("the fake application stopped"),
        }
    })
}

fn node(parent: Option<&str>, children: &[&str]) -> Node {
    Node {
        parent: parent.map(str::to_owned),
        children: children.iter().map(|c| (*c).to_owned()).collect(),
        role: Some(1),
        name: Some("held".into()),
        states: Some([0, 0]),
        interfaces: Some(1),
        attributes: Some(Vec::new()),
        ..Node::default()
    }
}

/// root, then /doc, which has /p1 and /p2, and /p1 has /p1a.
fn mirror() -> Mirror {
    let mut t = Tree::new(APP, ROOT_PATH);
    t.nodes.insert(ROOT_PATH.into(), node(None, &["/doc"]));
    t.nodes
        .insert("/doc".into(), node(Some(ROOT_PATH), &["/p1", "/p2"]));
    t.nodes.insert("/p1".into(), node(Some("/doc"), &["/p1a"]));
    t.nodes.insert("/p1a".into(), node(Some("/p1"), &[]));
    t.nodes.insert("/p2".into(), node(Some("/doc"), &[]));
    t.renumber_all();
    Mirror::new(t)
}

#[test]
fn a_timed_out_child_list_is_unknown_not_empty() {
    rig();
    let answer: Answer = Box::new(|call| match (path(call).as_str(), member(call).as_str()) {
        ("/doc", "GetChildren") => children(call, &["/p1", "/p2"]),
        ("/p1", "GetChildren") => None,
        (_, "GetChildren") => children(call, &[]),
        _ => data(call),
    });
    let res = against(Duration::from_millis(300), answer, async |c| {
        fetch::walk(c, "/doc", 16, WalkFields::ITEM, None, LIMITS).await
    });
    assert_eq!(
        res.failure("/p1", Field::Children),
        Some(&FailureKind::Timeout)
    );
    assert_eq!(res.failure("/p2", Field::Children), None);
    assert_eq!(res.errors.get("GetChildren"), Some(&1));
    let f = &res.failures[0];
    assert_eq!(
        (f.app.as_str(), f.path.as_str(), f.member.as_str()),
        (APP, "/p1", "GetChildren")
    );
    assert!(!res.truncated);
    let mut m = mirror();
    m.admit_subtree(res);
    assert_eq!(m.tree.nodes["/p1"].children, vec!["/p1a"]);
    assert_eq!(m.tree.nodes["/p1"].name.as_deref(), Some("name"));
    assert_eq!(m.tree.reachable().len(), m.tree.len());
    m.start_batch();
    assert_eq!(m.due_retries(), vec![Repair::FetchSubtree("/p1".into())]);
}

#[test]
fn a_walk_after_a_change_in_the_same_batch_does_not_undo_it() {
    rig();
    let r = |p: &str| ObjRef {
        bus: APP.into(),
        path: p.into(),
    };
    let ev = |member: &str, minor: &str, path: &str, any: AnyData| Event {
        sender: APP.into(),
        path: path.into(),
        class: "Object".into(),
        member: member.into(),
        minor: minor.into(),
        detail1: 0,
        detail2: 0,
        any,
    };
    let mut m = mirror();
    m.start_batch();
    // As a batch is applied: its cache items first, then its events in the
    // order they came.
    m.apply_add_accessible(CacheItem {
        object: r("/x"),
        app: r(ROOT_PATH),
        parent: r("/p2"),
        index_in_parent: 0,
        child_count: 0,
        interfaces: 0,
        name: "old".into(),
        role: 73,
        description: String::new(),
        states: [0, 0],
    });
    let mut repairs = m.apply_event(&ev("ChildrenChanged", "add", "/p2", AnyData::Ref(r("/x"))));
    repairs.extend(m.apply_event(&ev(
        "PropertyChange",
        "accessible-name",
        "/x",
        AnyData::Str("new".into()),
    )));
    assert!(repairs.contains(&Repair::FetchSubtree("/x".into())));
    let answer: Answer = Box::new(|call| match member(call).as_str() {
        "GetChildren" => children(call, &[]),
        "Get" => reply(call, &Variant::new("new")),
        _ => data(call),
    });
    let res = against(Duration::from_secs(5), answer, async |c| {
        fetch::walk(c, "/x", 16, WalkFields::ALL, Some(m.pool()), LIMITS).await
    });
    m.admit_subtree(res);
    assert_eq!(m.tree.nodes["/x"].name.as_deref(), Some("new"));
    assert!(!m.tree.nodes["/x"].children_unknown);
}

#[test]
fn a_walk_of_an_application_that_never_answers_changes_nothing() {
    rig();
    let res = against(Duration::from_millis(300), Box::new(|_| None), async |c| {
        fetch::walk(c, "/doc", 16, WalkFields::ALL, None, LIMITS).await
    });
    let mut m = mirror();
    let before = m.tree.clone();
    m.admit_subtree(res);
    assert_eq!(m.tree.nodes, before.nodes);
}

#[test]
fn an_error_reply_for_a_gone_object_says_so() {
    rig();
    let answer: Answer = Box::new(|call| match member(call).as_str() {
        "GetChildren" => error(call, "org.freedesktop.DBus.Error.UnknownObject"),
        _ => error(call, "org.freedesktop.DBus.Error.Failed"),
    });
    let res = against(Duration::from_secs(5), answer, async |c| {
        fetch::walk(c, "/doc", 16, WalkFields::ITEM, None, LIMITS).await
    });
    assert!(
        res.failure("/doc", Field::Children)
            .is_some_and(FailureKind::is_gone)
    );
    assert!(
        res.failure("/doc", Field::Role)
            .is_some_and(|k| !k.is_gone() && !k.worth_retrying())
    );
}

/// An application that lists two new objects under every object it is
/// asked about.
fn inventing() -> Answer {
    let next = std::cell::Cell::new(0u64);
    Box::new(move |call| match member(call).as_str() {
        "GetChildren" => {
            let n = next.get();
            next.set(n + 2);
            let a = format!("/o{n}");
            let b = format!("/o{}", n + 1);
            children(call, &[&a, &b])
        }
        _ => data(call),
    })
}

#[test]
fn a_walk_of_an_endless_tree_stops_at_its_object_limit() {
    rig();
    let limits = WalkLimits {
        max_objects: 500,
        ..LIMITS
    };
    let res = against(Duration::from_secs(5), inventing(), async |c| {
        fetch::walk(c, "/root", 16, WalkFields::STRUCTURE, None, limits).await
    });
    assert!(res.truncated);
    assert!(res.tree.len() <= 500, "{}", res.tree.len());
    assert!(
        res.failed
            .values()
            .any(|f| f.get(&Field::Children) == Some(&FailureKind::OverLimit))
    );
}

#[test]
fn a_walk_stops_at_its_deadline() {
    rig();
    let limits = WalkLimits {
        max_objects: usize::MAX,
        max_time: Duration::from_millis(300),
    };
    let t = Instant::now();
    let res = against(Duration::from_secs(30), inventing(), async |c| {
        fetch::walk(c, "/root", 16, WalkFields::STRUCTURE, None, limits).await
    });
    assert!(t.elapsed() < Duration::from_secs(3), "{:?}", t.elapsed());
    assert!(res.truncated);
    // Against one that answers nothing, the deadline still holds, though
    // each call would wait thirty seconds.
    let t = Instant::now();
    let res = against(Duration::from_secs(30), Box::new(|_| None), async |c| {
        fetch::walk(c, "/root", 16, WalkFields::ALL, None, limits).await
    });
    assert!(t.elapsed() < Duration::from_secs(3), "{:?}", t.elapsed());
    assert!(res.truncated);
    assert!(matches!(
        res.failure("/root", Field::Children),
        Some(FailureKind::NotMade { .. })
    ));
}

#[test]
fn waiting_for_activation_does_not_block_the_thread() {
    rig();
    let start = Instant::now();
    type Item<'a> = (
        (&'a str, ObjectPath<'a>),
        (&'a str, ObjectPath<'a>),
        (&'a str, ObjectPath<'a>),
        i32,
        i32,
        Vec<&'a str>,
        &'a str,
        u32,
        &'a str,
        Vec<u32>,
    );
    let answer: Answer = Box::new(move |call| {
        if start.elapsed() < Duration::from_millis(400) {
            error(call, "org.freedesktop.DBus.Error.UnknownMethod")
        } else {
            reply(call, &Vec::<Item<'_>>::new())
        }
    });
    let ticks = std::cell::Cell::new(0u32);
    let items = against(Duration::from_secs(5), answer, async |c| {
        let ticking = async {
            loop {
                ticks.set(ticks.get() + 1);
                time::sleep(Duration::from_millis(5)).unwrap().await;
            }
        };
        let waiting = app::get_items_once_active(c, Duration::from_secs(5));
        match select(std::pin::pin!(waiting), std::pin::pin!(ticking)).await {
            Either::Left((r, _)) => r,
            Either::Right(_) => unreachable!(),
        }
    });
    assert!(items.unwrap().is_empty());
    assert!(ticks.get() > 30, "only {} ticks in 400 ms", ticks.get());
}

#[test]
fn looking_for_a_document_in_a_cyclic_tree_ends() {
    rig();
    // The root lists a frame, and the frame lists the root.
    let answer: Answer = Box::new(|call| match (path(call).as_str(), member(call).as_str()) {
        (_, "GetRole") => reply(call, &50u32),
        (ROOT_PATH, "GetChildren") => children(call, &["/frame", ROOT_PATH]),
        (_, "GetChildren") => children(call, &[ROOT_PATH, "/frame"]),
        _ => data(call),
    });
    let found = against(Duration::from_secs(5), answer, async |c| {
        time::within(
            Duration::from_secs(3),
            "looking",
            app::find_document(c, "x"),
        )
        .await
    });
    assert!(matches!(found, Ok(Ok(None))), "{found:?}");
}

fn me() -> Peer {
    Peer {
        pid: std::process::id(),
        uid: my_uid(),
    }
}

fn my_uid() -> u32 {
    std::fs::read_to_string("/proc/self/status")
        .unwrap()
        .lines()
        .find_map(|l| l.strip_prefix("Uid:"))
        .and_then(|l| l.split_whitespace().next())
        .and_then(|u| u.parse().ok())
        .unwrap()
}

fn timeouts(connect_ms: u64) -> Timeouts {
    Timeouts {
        method: Duration::from_secs(30),
        connect: Duration::from_millis(connect_ms),
    }
}

/// A listener that accepts and then never says a word, and what it was
/// sent.
fn silent_listener() -> (String, std::sync::mpsc::Receiver<Vec<u8>>) {
    static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!("peer-test-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let sock = dir.join(format!("silent{n}"));
    let listener = UnixListener::bind(&sock).unwrap();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        if let Ok((mut s, _)) = listener.accept() {
            s.set_read_timeout(Some(Duration::from_millis(500)))
                .unwrap();
            let mut got = Vec::new();
            let mut buf = [0u8; 256];
            while let Ok(n) = s.read(&mut buf) {
                if n == 0 {
                    break;
                }
                got.extend_from_slice(&buf[..n]);
            }
            let _ = tx.send(got);
            std::thread::sleep(Duration::from_secs(5));
        }
    });
    (format!("unix:path={}", sock.display()), rx)
}

#[test]
fn a_server_that_never_finishes_the_handshake_is_given_up_on() {
    rig();
    let (address, _) = silent_listener();
    let t = Instant::now();
    let r = zbus::block_on(discovery::connect_p2p(&address, me(), timeouts(300)));
    assert!(matches!(r, Err(Error::TimedOut(_))), "{r:?}");
    assert!(t.elapsed() < Duration::from_secs(3), "{:?}", t.elapsed());
    let (address, _) = silent_listener();
    let t = Instant::now();
    let r = zbus::block_on(discovery::connect_bus(&address, timeouts(300)));
    assert!(matches!(r, Err(Error::TimedOut(_))), "{r:?}");
    assert!(t.elapsed() < Duration::from_secs(3), "{:?}", t.elapsed());
}

#[test]
fn a_server_of_another_process_is_refused_before_anything_is_sent() {
    rig();
    let (address, sent) = silent_listener();
    let other = Peer {
        pid: me().pid + 1,
        ..me()
    };
    let r = zbus::block_on(discovery::connect_p2p(&address, other, timeouts(3000)));
    assert!(matches!(r, Err(Error::Refused(_))), "{r:?}");
    assert_eq!(
        sent.recv_timeout(Duration::from_secs(3)).unwrap(),
        Vec::<u8>::new()
    );
}

#[test]
fn a_tcp_address_is_refused_and_its_nonce_file_never_read() {
    rig();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let secret = std::env::temp_dir().join(format!("nonce-{}", std::process::id()));
    std::fs::write(&secret, b"not for the application").unwrap();
    for address in [
        format!(
            "nonce-tcp:host=127.0.0.1,port={port},noncefile={}",
            secret.display()
        ),
        format!("tcp:host=127.0.0.1,port={port}"),
    ] {
        let r = zbus::block_on(discovery::connect_p2p(&address, me(), timeouts(1000)));
        assert!(matches!(r, Err(Error::Refused(_))), "{address}: {r:?}");
        let r = zbus::block_on(discovery::connect_bus(&address, timeouts(1000)));
        assert!(matches!(r, Err(Error::Refused(_))), "{address}: {r:?}");
    }
    std::thread::sleep(Duration::from_millis(100));
    assert!(listener.accept().is_err(), "something connected");
}

#[test]
fn the_applications_own_server_is_connected_to() {
    rig();
    let dir = std::env::temp_dir().join(format!("peer-ok-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("bus");
    let listener = UnixListener::bind(&sock).unwrap();
    let address = format!("unix:path={}", sock.display());
    // The listener's accept blocks, so the application runs on a thread.
    std::thread::spawn(move || {
        let (s, _) = listener.accept().unwrap();
        zbus::block_on(async {
            let conn = zbus::connection::Builder::unix_stream(s)
                .server(zbus::Guid::generate())
                .p2p()
                .build()
                .await
                .unwrap();
            serve(conn, Box::new(|call| reply(call, &95u32))).await
        });
    });
    let role = zbus::block_on(async {
        let conn = discovery::connect_p2p(&address, me(), timeouts(5000))
            .await
            .unwrap();
        let c = Client::p2p(conn, APP);
        let msg = c
            .request(ROOT_PATH, "org.a11y.atspi.Accessible", "GetRole", &())
            .await
            .unwrap();
        mirror::decode::role(&msg).unwrap()
    });
    assert_eq!(role, 95);
}
