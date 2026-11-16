//! Part one: the cost of loading an application's tree for the first time,
//! and of fetching it again by each method once it is loaded.

use std::cell::Cell;
use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use futures_util::future::join3;
use mirror::app;
use mirror::client::{Client, Counters, Transport};
use mirror::consts::{IFACE_ACCESSIBLE, ROOT_PATH};
use mirror::fetch::{self, WalkFields};
use mirror::tree::{self, Compared, ObjectSetComparison, Tree, TreeDiff};
use serde::Serialize;
use serde_json::{Value, json};

use crate::procstat::{Cost, Meter};
use crate::{Args, PeerToPeer, WALK_LIMITS, sleep, unix_time, write_json};

/// What a virtual buffer needs from GetTree; the bridge returns every
/// property of every interface when the list is empty.
const TREE_PROPERTIES: [&str; 5] = ["Name", "Description", "Parent", "ChildCount", "Attributes"];

/// The window for the methods that fill in what GetItems leaves out, and for
/// the walk that fetches the same fields as GetItems. Four to sixteen calls
/// in flight already keep Firefox's main thread busy.
const FILL_WINDOW: usize = 16;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Method {
    /// Every field a virtual buffer needs, attributes and text included.
    Walk(usize),
    /// Only the fields GetItems returns, so the two compare with equal data.
    WalkItemFields(usize),
    Items,
    ItemsSupplemented(usize),
    GetTree,
    /// A full walk while single calls on the side measure how long an
    /// interactive call waits behind it.
    Latency(usize),
}

impl Method {
    fn label(self) -> String {
        match self {
            Method::Walk(w) => format!("walk-{w}"),
            Method::WalkItemFields(w) => format!("walk-itemfields-{w}"),
            Method::Items => "getitems".into(),
            Method::ItemsSupplemented(w) => format!("getitems+attrs+text-{w}"),
            Method::GetTree => "gettree".into(),
            Method::Latency(w) => format!("latency-{w}"),
        }
    }

    fn window(self) -> Option<usize> {
        match self {
            Method::Walk(w)
            | Method::WalkItemFields(w)
            | Method::ItemsSupplemented(w)
            | Method::Latency(w) => Some(w),
            _ => None,
        }
    }

    fn compared(self) -> Compared {
        match self {
            Method::Walk(_) | Method::ItemsSupplemented(_) | Method::Latency(_) => Compared::ALL,
            Method::WalkItemFields(_) | Method::Items => Compared::ITEM,
            Method::GetTree => Compared::TREE,
        }
    }
}

const METHODS: [Method; 11] = [
    Method::Walk(1),
    Method::Walk(2),
    Method::Walk(4),
    Method::Walk(8),
    Method::Walk(16),
    Method::Walk(64),
    Method::Walk(256),
    Method::WalkItemFields(FILL_WINDOW),
    Method::Items,
    Method::ItemsSupplemented(FILL_WINDOW),
    Method::GetTree,
];

/// Measured over peer-to-peer only, the transport the engine will use.
const LATENCY_WINDOWS: [usize; 4] = [4, 16, 64, 256];

#[derive(Serialize)]
struct MethodResult {
    method: String,
    window: Option<usize>,
    transport: Transport,
    cost: Cost,
    objects: usize,
    text_objects: usize,
    call_errors: BTreeMap<&'static str, usize>,
    same_objects_as_reference: Option<ObjectSetComparison>,
    differences_from_reference: Option<TreeDiff>,
    details: BTreeMap<&'static str, Value>,
    error: Option<String>,
}

struct Fetched {
    tree: Tree,
    call_errors: BTreeMap<&'static str, usize>,
    details: BTreeMap<&'static str, Value>,
}

/// Wall time of each probe call, in seconds.
#[derive(Default)]
struct Probe {
    samples: Vec<f64>,
    errors: usize,
    timer_failed: bool,
}

impl Probe {
    fn summary(&self) -> Value {
        let mut s = self.samples.clone();
        s.sort_by(f64::total_cmp);
        let q = |p: f64| -> Value {
            if s.is_empty() {
                return Value::Null;
            }
            let i = ((s.len() - 1) as f64 * p).round() as usize;
            json!((s[i] * 1e6).round() / 1e3)
        };
        json!({
            "calls": s.len(),
            "errors": self.errors,
            "timer_failed": self.timer_failed,
            "median_ms": q(0.5),
            "p90_ms": q(0.9),
            "p99_ms": q(0.99),
            "max_ms": q(1.0),
        })
    }
}

/// Sends one GetRole at a time until `done`, pausing between calls so that
/// the probe never adds more than one call to what the application has
/// queued.
async fn probe(client: &Client, done: &Cell<bool>, limit: Option<usize>) -> Probe {
    let mut p = Probe::default();
    while !done.get() && limit.is_none_or(|n| p.samples.len() + p.errors < n) {
        let t = Instant::now();
        match client
            .call(ROOT_PATH, IFACE_ACCESSIBLE, "GetRole", &())
            .await
        {
            Ok(_) => p.samples.push(t.elapsed().as_secs_f64()),
            Err(_) => p.errors += 1,
        }
        if sleep(Duration::from_millis(5)).await.is_err() {
            p.timer_failed = true;
            break;
        }
    }
    p
}

async fn fetch_by(
    method: Method,
    client: &Client,
    side: Option<&Client>,
) -> Result<Fetched, String> {
    let mut details = BTreeMap::new();
    let mut call_errors = BTreeMap::new();
    let tree = match method {
        Method::Walk(w) | Method::WalkItemFields(w) => {
            let fields = if matches!(method, Method::Walk(_)) {
                WalkFields::ALL
            } else {
                WalkFields::ITEM
            };
            let r = fetch::walk(client, ROOT_PATH, w, fields, None, WALK_LIMITS).await;
            call_errors = r.errors;
            details.insert("foreign_children", json!(r.foreign_children));
            details.insert("truncated", json!(r.truncated));
            details.insert("failures", json!(r.failures));
            r.tree
        }
        Method::Items => {
            let items = fetch::get_items(client).await.map_err(|e| e.to_string())?;
            details.insert("items", json!(items.len()));
            Tree::from_items(&client.app_bus, &items)
        }
        Method::ItemsSupplemented(w) => {
            let items = fetch::get_items(client).await.map_err(|e| e.to_string())?;
            let mut t = Tree::from_items(&client.app_bus, &items);
            let s = fetch::supplement(client, &mut t, w, WalkFields::ALL).await;
            call_errors = s.errors;
            details.insert("failures", json!(s.failures));
            t
        }
        Method::GetTree => {
            let entries = fetch::get_tree(client, ROOT_PATH, &TREE_PROPERTIES)
                .await
                .map_err(|e| e.to_string())?;
            details.insert("properties", json!(TREE_PROPERTIES));
            Tree::from_tree_entries(&client.app_bus, ROOT_PATH, &entries)
        }
        Method::Latency(w) => {
            let side = side.ok_or("no second peer-to-peer connection for the probe")?;
            let idle_same = probe(client, &Cell::new(false), Some(100)).await;
            let idle_side = probe(side, &Cell::new(false), Some(100)).await;
            let done = Cell::new(false);
            let walk = async {
                let r = fetch::walk(client, ROOT_PATH, w, WalkFields::ALL, None, WALK_LIMITS).await;
                done.set(true);
                r
            };
            let (r, same, other) =
                join3(walk, probe(client, &done, None), probe(side, &done, None)).await;
            call_errors = r.errors;
            details.insert("truncated", json!(r.truncated));
            details.insert(
                "probe",
                json!({
                    "call": "GetRole on the application root, one at a time",
                    "idle_same_connection": idle_same.summary(),
                    "idle_second_connection": idle_side.summary(),
                    "loaded_same_connection": same.summary(),
                    "loaded_second_connection": other.summary(),
                }),
            );
            r.tree
        }
    };
    Ok(Fetched {
        tree,
        call_errors,
        details,
    })
}

async fn measure(
    method: Method,
    client: &Client,
    side: Option<&Client>,
    pid: u32,
    reference: Option<&Tree>,
) -> (MethodResult, Option<Tree>) {
    // Lets the application go idle, and its timers fire, outside the timing.
    let paused = sleep(Duration::from_millis(300)).await;
    let meter = Meter::start(pid, client.counters());
    let r = match paused {
        Ok(()) => fetch_by(method, client, side).await,
        Err(e) => Err(e),
    };
    let cost = meter.stop(client.counters());
    let mut res = MethodResult {
        method: method.label(),
        window: method.window(),
        transport: client.transport,
        cost,
        objects: 0,
        text_objects: 0,
        call_errors: BTreeMap::new(),
        same_objects_as_reference: None,
        differences_from_reference: None,
        details: BTreeMap::new(),
        error: None,
    };
    match r {
        Err(e) => {
            res.error = Some(e);
            (res, None)
        }
        Ok(f) => {
            res.objects = f.tree.len();
            res.text_objects = f.tree.nodes.values().filter(|n| n.text.is_some()).count();
            res.call_errors = f.call_errors;
            res.details = f.details;
            if matches!(method, Method::Items | Method::ItemsSupplemented(_)) {
                let minus_one = f
                    .tree
                    .nodes
                    .values()
                    .filter(|n| n.child_count == -1)
                    .count();
                let unreachable = f.tree.len() - f.tree.reachable().len();
                res.details
                    .insert("child_count_minus_one", json!(minus_one));
                res.details
                    .insert("unreachable_from_root", json!(unreachable));
            }
            if let Some(r) = reference {
                res.same_objects_as_reference = Some(r.same_objects(&f.tree));
                res.differences_from_reference = Some(tree::diff(r, &f.tree, method.compared()));
            }
            (res, Some(f.tree))
        }
    }
}

/// The first contact with an application: activating its bridge, opening the
/// peer-to-peer connection, and the first GetItems.
#[derive(Serialize)]
struct Load {
    /// Whether the wait step walked the document first, so that its objects
    /// already existed when the bridge was activated.
    wait_mode: String,
    /// Activation through the first GetItems, decoded into a tree.
    total: Cost,
    activation: Cost,
    p2p_address_offered: bool,
    p2p_connect_s: Option<f64>,
    /// Why calls went over the bus although a server was offered.
    p2p_fell_back: Option<String>,
    first_getitems: Cost,
    first_getitems_transport: Transport,
    first_getitems_objects: usize,
    /// Against the sequential walk taken later, which shows whether the page
    /// was complete when it was loaded.
    first_getitems_vs_reference: Option<ObjectSetComparison>,
    first_getitems_differences: Option<TreeDiff>,
}

fn transport_name(c: &Client) -> &'static str {
    match c.transport {
        Transport::Bus => "bus",
        Transport::PeerToPeer => "peer-to-peer",
    }
}

/// SplitMix64, so that a run's order follows from its seed alone.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }

    fn shuffle<T>(&mut self, v: &mut [T]) {
        for i in (1..v.len()).rev() {
            let j = (self.next() % (i as u64 + 1)) as usize;
            v.swap(i, j);
        }
    }
}

pub async fn run(args: &Args) -> Result<(), String> {
    let title = args.get("title")?;
    let page = args.get("page")?;
    let out = args.get("out")?;
    let run_index = args.num("run-index", 0)? as usize;
    let seed = args.num("seed", run_index as u64 + 1)?;
    let wait = crate::wait_info(args);
    let wait_mode = wait
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_owned();
    let only: Option<Vec<&str>> = args.0.get("methods").map(|s| s.split(',').collect());
    let target = crate::connect(args).await?;
    let info = target.info(title).await;
    let bus_client = target.bus_client();

    let total = Meter::start(target.pid, bus_client.counters());
    let meter = Meter::start(target.pid, bus_client.counters());
    let address = app::activate(&bus_client).await.map_err(crate::err)?;
    let activation = meter.stop(bus_client.counters());
    let mut p2p = PeerToPeer::default();
    let p2p_client = target.p2p_client(&address, &mut p2p).await;
    let main_client = p2p_client.as_ref().unwrap_or(&bus_client);
    let meter = Meter::start(target.pid, main_client.counters());
    let first = app::get_items_once_active(main_client, Duration::from_secs(10))
        .await
        .map_err(crate::err)?;
    let first_tree = Tree::from_items(&target.app_bus, &first);
    let first_getitems = meter.stop(main_client.counters());
    let both = |c: Counters| match &p2p_client {
        Some(p) => c + p.counters(),
        None => c,
    };
    let total = total.stop(both(bus_client.counters()));
    let mut load = Load {
        wait_mode,
        total,
        activation,
        p2p_address_offered: p2p.address_offered,
        p2p_connect_s: p2p.connect_s,
        p2p_fell_back: p2p.fell_back,
        first_getitems,
        first_getitems_transport: main_client.transport,
        first_getitems_objects: first_tree.len(),
        first_getitems_vs_reference: None,
        first_getitems_differences: None,
    };

    let mut warmup = Vec::new();
    let mut results = Vec::new();
    let mut reference = None;
    let mut order = Vec::new();
    let mut second = PeerToPeer::default();
    if only.as_ref().is_none_or(|o| !o.contains(&"none")) {
        for m in [Method::Walk(64), Method::Items] {
            warmup.push(measure(m, main_client, None, target.pid, None).await.0);
        }
        let (reference_result, walked) =
            measure(Method::Walk(1), main_client, None, target.pid, None).await;
        let walked = walked.ok_or("the reference walk failed")?;
        results.push(reference_result);
        load.first_getitems_vs_reference = Some(walked.same_objects(&first_tree));
        load.first_getitems_differences = Some(tree::diff(&walked, &first_tree, Compared::ITEM));

        // A probe without its second connection reports the method as
        // failed; the run itself goes on.
        let side = match &p2p_client {
            Some(_) => target.p2p_client(&address, &mut second).await,
            None => None,
        };
        let mut plan: Vec<(Method, &Client)> = Vec::new();
        for client in [p2p_client.as_ref(), Some(&bus_client)]
            .into_iter()
            .flatten()
        {
            for m in METHODS {
                if m == Method::Walk(1) && std::ptr::eq(client, main_client) {
                    continue;
                }
                plan.push((m, client));
            }
        }
        if let Some(p) = &p2p_client {
            plan.extend(LATENCY_WINDOWS.iter().map(|&w| (Method::Latency(w), p)));
        }
        plan.retain(|(m, _)| {
            only.as_ref()
                .is_none_or(|o| o.contains(&m.label().as_str()))
        });
        Rng(seed).shuffle(&mut plan);
        for (m, client) in plan {
            order.push(format!("{} over {}", m.label(), transport_name(client)));
            let r = measure(m, client, side.as_ref(), target.pid, Some(&walked)).await;
            results.push(r.0);
        }
        reference = Some(walked);
    }

    let document_objects = match (&reference, &info.document) {
        (Some(r), Some(doc)) => Some(r.subtree_len(doc)),
        _ => None,
    };
    write_json(
        out,
        &json!({
            "schema": "basedreader-bench/fetch/2",
            "page": page,
            "title": title,
            "run_index": run_index,
            "seed": seed,
            "unix_time": unix_time(),
            "wait": wait,
            "target": info,
            "load": load,
            "second_connection": second,
            "warmup": warmup,
            "reference": format!("walk-1 over {}", transport_name(main_client)),
            "application_objects": reference.as_ref().map(Tree::len),
            "document_objects": document_objects,
            "order": order,
            "methods": results,
        }),
    )
}
