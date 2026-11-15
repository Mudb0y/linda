//! Part one: the cost of fetching the whole tree by each method.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use mirror::client::{Client, Transport};
use mirror::consts::ROOT_PATH;
use mirror::fetch::{self, WalkFields};
use mirror::tree::{self, ObjectSetComparison, Tree, TreeDiff};
use mirror::{app, discovery};
use serde::Serialize;
use serde_json::{Value, json};

use crate::procstat::{Cost, Meter};
use crate::{Args, delay, unix_time, write_json};

/// What a virtual buffer needs from GetTree; the bridge returns every
/// property of every interface when the list is empty.
const TREE_PROPERTIES: [&str; 5] = ["Name", "Description", "Parent", "ChildCount", "Attributes"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Method {
    Walk(usize),
    Items,
    ItemsSupplemented(usize),
    GetTree,
}

impl Method {
    fn label(self) -> String {
        match self {
            Method::Walk(w) => format!("walk-{w}"),
            Method::Items => "getitems".into(),
            Method::ItemsSupplemented(w) => format!("getitems+attrs+text-{w}"),
            Method::GetTree => "gettree".into(),
        }
    }

    fn window(self) -> Option<usize> {
        match self {
            Method::Walk(w) | Method::ItemsSupplemented(w) => Some(w),
            _ => None,
        }
    }
}

const METHODS: [Method; 7] = [
    Method::Walk(1),
    Method::Walk(16),
    Method::Walk(64),
    Method::Walk(256),
    Method::Items,
    Method::ItemsSupplemented(64),
    Method::GetTree,
];

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

async fn fetch_by(method: Method, client: &Client) -> Result<Fetched, String> {
    let mut details = BTreeMap::new();
    let mut call_errors = BTreeMap::new();
    let tree = match method {
        Method::Walk(w) => {
            let r = fetch::walk(client, ROOT_PATH, w, WalkFields::ALL, None).await;
            call_errors = r.errors;
            details.insert("foreign_children", json!(r.foreign_children));
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
            call_errors = fetch::supplement(client, &mut t, w, WalkFields::ALL).await;
            t
        }
        Method::GetTree => {
            let entries = fetch::get_tree(client, ROOT_PATH, &TREE_PROPERTIES)
                .await
                .map_err(|e| e.to_string())?;
            details.insert("properties", json!(TREE_PROPERTIES));
            Tree::from_tree_entries(&client.app_bus, ROOT_PATH, &entries)
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
    pid: u32,
    reference: Option<&Tree>,
) -> (MethodResult, Option<Tree>) {
    // Lets the application go idle, and its timers fire, outside the timing.
    delay::sleep(Duration::from_millis(300)).await;
    let meter = Meter::start(pid, client.counters());
    let r = fetch_by(method, client).await;
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
                res.differences_from_reference = Some(tree::diff(r, &f.tree));
            }
            (res, Some(f.tree))
        }
    }
}

#[derive(Serialize)]
struct Activation {
    cost: Cost,
    p2p_address_offered: bool,
    getitems_ready_after_s: f64,
    first_getitems_objects: usize,
    p2p_connect_s: Option<f64>,
}

pub async fn run(args: &Args) -> Result<(), String> {
    let title = args.get("title")?;
    let page = args.get("page")?;
    let out = args.get("out")?;
    let run_index = args.num("run-index", 0)? as usize;
    let target = crate::connect(args).await?;
    let info = target.info(title).await;
    let bus_client = target.bus_client();

    let meter = Meter::start(target.pid, bus_client.counters());
    let address = app::activate(&bus_client).await.map_err(crate::err)?;
    let activation_cost = meter.stop(bus_client.counters());
    let t = Instant::now();
    let first = app::get_items_once_active(&bus_client, Duration::from_secs(10))
        .await
        .map_err(crate::err)?;
    let ready_after = t.elapsed().as_secs_f64();
    let mut p2p_connect_s = None;
    let p2p_client = if address.is_empty() {
        None
    } else {
        let t = Instant::now();
        let conn = discovery::connect_p2p(&address, target.method_timeout)
            .await
            .map_err(|e| format!("peer-to-peer connection to {address}: {e}"))?;
        p2p_connect_s = Some(t.elapsed().as_secs_f64());
        Some(Client::p2p(conn, &target.app_bus))
    };
    let activation = Activation {
        cost: activation_cost,
        p2p_address_offered: !address.is_empty(),
        getitems_ready_after_s: ready_after,
        first_getitems_objects: first.len(),
        p2p_connect_s,
    };

    let main_client = p2p_client.as_ref().unwrap_or(&bus_client);
    let mut warmup = Vec::new();
    for m in [Method::Walk(64), Method::Items] {
        warmup.push(measure(m, main_client, target.pid, None).await.0);
    }

    let (reference_result, reference) =
        measure(Method::Walk(1), main_client, target.pid, None).await;
    let reference = reference.ok_or("the reference walk failed")?;
    let mut results = vec![reference_result];

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
    let only: Option<Vec<&str>> = args.0.get("methods").map(|s| s.split(',').collect());
    plan.retain(|(m, _)| {
        only.as_ref()
            .is_none_or(|o| o.contains(&m.label().as_str()))
    });
    if !plan.is_empty() {
        let k = run_index % plan.len();
        plan.rotate_left(k);
    }
    for (m, client) in plan {
        results.push(measure(m, client, target.pid, Some(&reference)).await.0);
    }

    write_json(
        out,
        &json!({
            "schema": "basedreader-bench/fetch/1",
            "page": page,
            "title": title,
            "run_index": run_index,
            "unix_time": unix_time(),
            "wait": crate::wait_info(args),
            "target": info,
            "activation": activation,
            "warmup": warmup,
            "reference": format!("walk-1 over {}", if p2p_client.is_some() { "peer-to-peer" } else { "bus" }),
            "reference_objects": reference.len(),
            "methods": results,
        }),
    )
}
