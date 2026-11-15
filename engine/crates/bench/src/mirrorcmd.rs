//! Part two: build a mirror from `GetItems`, keep it current from signals
//! while the page mutates on its schedule, then compare it with a fresh walk.

use std::collections::{BTreeMap, BTreeSet, HashSet, VecDeque};
use std::time::{Duration, Instant};

use futures_util::FutureExt;
use futures_util::future::{Either, select};
use futures_util::stream::StreamExt;
use mirror::client::Client;
use mirror::consts::{
    IFACE_ACCESSIBLE, IFACE_CACHE, IFACE_PROPERTIES, IFACE_TEXT, ROLE_HEADING, ROOT_PATH,
    state_name,
};
use mirror::decode;
use mirror::fetch::{self, WalkFields};
use mirror::maintain::{Mirror, NaiveMirror, Repair};
use mirror::tree::{self, Compared, Tree};
use mirror::{app, discovery};
use serde::Serialize;
use serde_json::json;
use zbus::message::Type;
use zbus::{MatchRule, Message, MessageStream};

use crate::delay::Delay;
use crate::procstat::Meter;
use crate::{Args, delay, unix_time, write_json};

const STATUS_PREFIX: &str = "Mutations ";

/// Calls in flight for the build and for repair walks; four to sixteen
/// already keep Firefox's main thread busy.
const REPAIR_WINDOW: usize = 16;

fn status(t: &Tree) -> Option<String> {
    t.nodes
        .values()
        .filter(|n| n.role == Some(ROLE_HEADING))
        .filter_map(|n| n.name.as_deref())
        .find(|s| s.starts_with(STATUS_PREFIX))
        .map(str::to_owned)
}

#[derive(Default, Serialize)]
struct LoopStats {
    batches: usize,
    largest_batch: usize,
    signals: usize,
    undecodable_signals: usize,
    repairs_run: usize,
    repairs_skipped_object_gone: usize,
    subtree_roots_gone: usize,
    objects_fetched_by_repairs: usize,
    repair_wall_s: f64,
    /// Calls made by repairs that failed or whose reply did not decode, by
    /// method. On a page that is changing, an object can go between the
    /// signal and the repair, so some failures are expected; what matters is
    /// whether the mirror still matches the walk at the end.
    repair_call_errors: BTreeMap<String, usize>,
    repair_timeouts: usize,
    repair_undecodable_replies: usize,
    /// Every signal, one line each, kept only when the run dumps its trees.
    #[serde(skip)]
    signal_log: Option<Vec<String>>,
    /// Every object a signal the mirror acts on came from or named.
    #[serde(skip)]
    signalled: HashSet<String>,
    /// Every object and state named by a StateChanged.
    #[serde(skip)]
    state_signals: HashSet<(String, String)>,
}

fn describe(m: &Message) -> String {
    let h = m.header();
    let member = h.member().map(|x| x.to_string()).unwrap_or_default();
    let path = h.path().map(|x| x.to_string()).unwrap_or_default();
    if let Ok(Some(e)) = decode::event(m) {
        return format!(
            "{}.{} {} {} {} {:?} on {}",
            e.class, e.member, e.minor, e.detail1, e.detail2, e.any, e.path
        );
    }
    match member.as_str() {
        "AddAccessible" => match decode::add_accessible(m) {
            Ok(it) => format!(
                "Cache.AddAccessible {} parent {} index {} role {}",
                it.object.path, it.parent.path, it.index_in_parent, it.role
            ),
            Err(_) => "Cache.AddAccessible undecodable".into(),
        },
        "RemoveAccessible" => match decode::remove_accessible(m) {
            Ok(r) => format!("Cache.RemoveAccessible {}", r.path),
            Err(_) => "Cache.RemoveAccessible undecodable".into(),
        },
        _ => format!("{member} on {path}"),
    }
}

impl LoopStats {
    fn call_failed(&mut self, method: &str, e: &zbus::Error) {
        *self
            .repair_call_errors
            .entry(method.to_owned())
            .or_default() += 1;
        if mirror::client::is_timeout(e) {
            self.repair_timeouts += 1;
        }
    }

    fn undecodable(&mut self, method: &str) {
        *self
            .repair_call_errors
            .entry(method.to_owned())
            .or_default() += 1;
        self.repair_undecodable_replies += 1;
    }
}

/// What the mirror fetches for every object, and so what the reference walk
/// fetches to compare with it.
const MIRROR_FIELDS: WalkFields = WalkFields::ALL;

async fn run_repairs(
    client: &Client,
    mirror: &mut Mirror,
    repairs: Vec<Repair>,
    stats: &mut LoopStats,
) {
    let mut seen = HashSet::new();
    let mut queue = VecDeque::new();
    let enqueue = |queue: &mut VecDeque<Repair>, seen: &mut HashSet<Repair>, rs: Vec<Repair>| {
        queue.extend(rs.into_iter().filter(|r| seen.insert(r.clone())));
    };
    enqueue(&mut queue, &mut seen, repairs);
    let t = Instant::now();
    while let Some(r) = queue.pop_front() {
        let path = r.path();
        let held = mirror.tree.nodes.contains_key(path);
        // Every repair but Locate needs the object in the mirror; Locate is
        // done once it is.
        if held == matches!(r, Repair::Locate(_)) {
            stats.repairs_skipped_object_gone += usize::from(!held);
            continue;
        }
        stats.repairs_run += 1;
        if let Some(log) = stats.signal_log.as_mut() {
            log.push(format!("repair {r:?}"));
        }
        match &r {
            Repair::RefetchChildren(p) => {
                match client.call(p, IFACE_ACCESSIBLE, "GetChildren", &()).await {
                    Ok(msg) => match decode::children(&msg) {
                        Ok(live) => {
                            let more = mirror.reconcile_children(p, &live);
                            enqueue(&mut queue, &mut seen, more);
                        }
                        Err(_) => stats.undecodable("GetChildren"),
                    },
                    Err(e) => {
                        stats.call_failed("GetChildren", &e);
                        if !mirror::client::is_timeout(&e) {
                            mirror.forget(p);
                        }
                    }
                }
            }
            Repair::FetchSubtree(p) => {
                let res =
                    fetch::walk(client, p, REPAIR_WINDOW, MIRROR_FIELDS, Some(mirror.pool())).await;
                for (method, n) in &res.errors {
                    *stats
                        .repair_call_errors
                        .entry((*method).to_owned())
                        .or_default() += n;
                }
                if res.children_failed.iter().any(|f| f == p) {
                    stats.subtree_roots_gone += 1;
                    mirror.forget(p);
                } else {
                    stats.objects_fetched_by_repairs += res.tree.len();
                    mirror.admit_subtree(res.tree);
                }
            }
            Repair::RefetchText(p) => {
                match client.call(p, IFACE_TEXT, "GetText", &(0i32, -1i32)).await {
                    Ok(msg) => match decode::string(&msg) {
                        Ok(text) => mirror.set_text(p, text),
                        Err(_) => stats.undecodable("Text.GetText"),
                    },
                    Err(e) => stats.call_failed("Text.GetText", &e),
                }
            }
            Repair::Locate(p) => {
                let parent = client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_ACCESSIBLE, "Parent"))
                    .await;
                match parent.map(|m| decode::ref_property(&m)) {
                    Ok(Ok(parent)) if parent.bus == client.app_bus && !parent.is_null() => {
                        let next = match mirror.tree.nodes.get(&parent.path) {
                            Some(n) if n.children.contains(p) => None,
                            Some(_) => Some(Repair::RefetchChildren(parent.path)),
                            None => Some(Repair::Locate(parent.path)),
                        };
                        enqueue(&mut queue, &mut seen, next.into_iter().collect());
                    }
                    Ok(Ok(_)) => {}
                    Ok(Err(_)) => stats.undecodable("Get(Parent)"),
                    // The object is gone, which is why the mirror lacks it.
                    Err(e) => stats.call_failed("Get(Parent)", &e),
                }
            }
            Repair::RefetchAttributes(p) => {
                match client.call(p, IFACE_ACCESSIBLE, "GetAttributes", &()).await {
                    Ok(msg) => match decode::attributes(&msg) {
                        Ok(a) => mirror.set_attributes(p, a),
                        Err(_) => stats.undecodable("GetAttributes"),
                    },
                    Err(e) => stats.call_failed("GetAttributes", &e),
                }
            }
        }
    }
    stats.repair_wall_s += t.elapsed().as_secs_f64();
}

/// Firefox's parent process fetches each kind of data from the content
/// process the first time a client asks for it, and the first GetAttributes
/// into a page answers empty while that happens; no signal follows when the
/// data arrives. A web document always has attributes, so asking it until
/// it answers makes every later answer complete. Returns the calls made.
async fn prime_attributes(client: &Client, document: &str) -> usize {
    for n in 1..=50 {
        if let Ok(msg) = client
            .call(document, IFACE_ACCESSIBLE, "GetAttributes", &())
            .await
            && decode::attributes(&msg).is_ok_and(|a| !a.is_empty())
        {
            return n;
        }
        delay::sleep(Duration::from_millis(10)).await;
    }
    50
}

/// Differences in one kind of field between the walk and the mirror, by
/// attribute name or state, split by whether a signal could have told the
/// mirror. For an attribute that is any signal from or naming the object or
/// its parent, since a structural change can change attributes; for a state
/// it is a StateChanged for that state from that object, the only way the
/// mirror learns of one. Without one the mirror had nothing to act on:
/// Firefox sends no signal when, for one, aria-roledescription changes, or
/// when layout moves an object into view and it gains the showing state.
#[derive(Default, Serialize)]
struct Drift {
    objects_with_signal: usize,
    objects_without_signal: usize,
    with_signal: BTreeMap<String, usize>,
    without_signal: BTreeMap<String, usize>,
}

impl Drift {
    fn add(&mut self, seen: bool, keys: impl IntoIterator<Item = String>) {
        let (objects, by_key) = if seen {
            (&mut self.objects_with_signal, &mut self.with_signal)
        } else {
            (&mut self.objects_without_signal, &mut self.without_signal)
        };
        *objects += 1;
        for k in keys {
            *by_key.entry(k).or_default() += 1;
        }
    }
}

#[derive(Default, Serialize)]
struct FieldDrift {
    attributes: Drift,
    states: Drift,
}

fn field_drift(reference: &Tree, mirror: &Tree, stats: &LoopStats) -> FieldDrift {
    let signalled = &stats.signalled;
    let mut d = FieldDrift::default();
    for (path, r) in &reference.nodes {
        let Some(m) = mirror.nodes.get(path) else {
            continue;
        };
        let seen =
            signalled.contains(path) || r.parent.as_ref().is_some_and(|p| signalled.contains(p));
        if let (Some(a), Some(b)) = (&r.attributes, &m.attributes)
            && a != b
        {
            let ma: BTreeMap<_, _> = a.iter().cloned().collect();
            let mb: BTreeMap<_, _> = b.iter().cloned().collect();
            let names: BTreeSet<&String> = ma.keys().chain(mb.keys()).collect();
            let keys = names
                .into_iter()
                .filter(|k| ma.get(*k) != mb.get(*k))
                .cloned();
            d.attributes.add(seen, keys);
        }
        if let (Some(a), Some(b)) = (r.states, m.states)
            && a != b
        {
            let names: Vec<String> = (0..64u32)
                .filter(|&i| (a[i as usize / 32] ^ b[i as usize / 32]) & (1 << (i % 32)) != 0)
                .map(|i| state_name(i).to_owned())
                .collect();
            let told = names
                .iter()
                .any(|n| stats.state_signals.contains(&(path.clone(), n.clone())));
            d.states.add(told, names);
        }
    }
    d
}

fn apply_batch(
    batch: &[Message],
    mirror: &mut Mirror,
    naive: &mut NaiveMirror,
    stats: &mut LoopStats,
) -> Vec<Repair> {
    mirror.start_batch();
    stats.batches += 1;
    stats.largest_batch = stats.largest_batch.max(batch.len());
    stats.signals += batch.len();
    for m in batch {
        match decode::event(m) {
            Ok(Some(e)) if e.minor == "accessible-parent" => {}
            Ok(Some(e)) => {
                if e.member == "StateChanged" {
                    stats
                        .state_signals
                        .insert((e.path.clone(), e.minor.clone()));
                }
                stats.signalled.insert(e.path);
                if let decode::AnyData::Ref(r) = e.any {
                    stats.signalled.insert(r.path);
                }
            }
            _ => {
                if let Ok(it) = decode::add_accessible(m) {
                    stats.signalled.insert(it.object.path);
                } else if let Some(p) = m.header().path() {
                    stats.signalled.insert(p.to_string());
                }
            }
        }
    }
    let n = stats.batches;
    if let Some(log) = stats.signal_log.as_mut() {
        log.extend(batch.iter().map(|m| format!("batch {n}: {}", describe(m))));
    }
    let is_cache = |m: &Message, member: &str| {
        let h = m.header();
        h.interface().is_some_and(|i| i.as_str() == IFACE_CACHE)
            && h.member().is_some_and(|x| x.as_str() == member)
    };
    // The bridge batches AddAccessible in an idle callback, so it can arrive
    // after the ChildrenChanged that admits the object. It is only data, so
    // taking all of a batch's items first loses nothing and saves calls.
    for m in batch.iter().filter(|m| is_cache(m, "AddAccessible")) {
        match decode::add_accessible(m) {
            Ok(item) => {
                naive.apply_add_accessible(&item);
                mirror.apply_add_accessible(item);
            }
            Err(_) => stats.undecodable_signals += 1,
        }
    }
    let mut repairs = Vec::new();
    for m in batch {
        if is_cache(m, "AddAccessible") {
            continue;
        }
        if is_cache(m, "RemoveAccessible") {
            match decode::remove_accessible(m) {
                Ok(r) => {
                    naive.apply_remove_accessible(&r);
                    mirror.apply_remove_accessible(&r);
                }
                Err(_) => stats.undecodable_signals += 1,
            }
            continue;
        }
        match decode::event(m) {
            Ok(Some(e)) => repairs.extend(mirror.apply_event(&e)),
            Ok(None) => {}
            Err(_) => stats.undecodable_signals += 1,
        }
    }
    repairs
}

pub async fn run(args: &Args) -> Result<(), String> {
    let title = args.get("title")?;
    let out = args.get("out")?;
    let watch = Duration::from_millis(args.num("watch-ms", 30_000)?);
    let run_index = args.num("run-index", 0)?;
    // How long to keep collecting signals after the first of a batch before
    // applying them, so that late AddAccessible items can stand in for calls.
    let coalesce = Duration::from_millis(args.num("coalesce-ms", 0)?);
    let t_start = Instant::now();
    let target = crate::connect(args).await?;
    let info = target.info(title).await;

    // Subscribed before activation and before the snapshot, so nothing that
    // changes after the snapshot is missed.
    let rule = MatchRule::builder()
        .msg_type(Type::Signal)
        .sender(target.app_bus.as_str())
        .build()
        .map_err(|e| e.to_string())?;
    let mut stream = MessageStream::for_match_rule(
        rule,
        &target.bus,
        Some(std::num::NonZeroUsize::new(1 << 20).expect("nonzero")),
    )
    .await
    .map_err(|e| format!("subscribing to signals: {e}"))?;
    for class in ["object:", "window:", "focus:", "document:"] {
        app::register_event(&target.bus, class)
            .await
            .map_err(|e| format!("registering {class}: {e}"))?;
    }
    let bus_client = target.bus_client();
    let address = app::activate(&bus_client).await.map_err(crate::err)?;
    let p2p_client = if address.is_empty() {
        None
    } else {
        let conn = discovery::connect_p2p(&address, target.method_timeout)
            .await
            .map_err(|e| format!("peer-to-peer connection to {address}: {e}"))?;
        Some(Client::p2p(conn, &target.app_bus))
    };
    let client = p2p_client.as_ref().unwrap_or(&bus_client);

    let t_build = Instant::now();
    let meter = Meter::start(target.pid, client.counters());
    let attribute_priming_calls = match &info.document {
        Some(doc) => prime_attributes(client, doc).await,
        None => 0,
    };
    let items = app::get_items_once_active(client, Duration::from_secs(10))
        .await
        .map_err(crate::err)?;
    let mut tree = Tree::from_items(&target.app_bus, &items);
    let naive_tree = tree.clone();
    let pruned_at_build = tree.prune_unreachable();
    let build_errors = fetch::supplement(client, &mut tree, REPAIR_WINDOW, MIRROR_FIELDS).await;
    let build_cost = meter.stop(client.counters());
    let empty_attributes: Vec<&String> = tree
        .nodes
        .iter()
        .filter(|(_, n)| n.attributes.as_ref().is_some_and(Vec::is_empty))
        .map(|(p, _)| p)
        .collect();
    let empty_attributes = json!(empty_attributes);
    let status_at_build = status(&tree);
    let build_objects = tree.len();
    let mut mirror = Mirror::new(tree);
    let mut naive = NaiveMirror { tree: naive_tree };
    let build_s = t_build.elapsed().as_secs_f64();
    let watch_left = watch.saturating_sub(t_start.elapsed());

    let mut stats = LoopStats {
        signal_log: args.0.contains_key("dump").then(Vec::new),
        ..LoopStats::default()
    };
    let meter = Meter::start(target.pid, client.counters());
    let mut deadline = Delay::new(watch_left);
    loop {
        let first = match select(stream.next(), &mut deadline).await {
            Either::Left((Some(Ok(m)), _)) => m,
            Either::Left((Some(Err(_)), _)) => {
                stats.undecodable_signals += 1;
                continue;
            }
            Either::Left((None, _)) | Either::Right(_) => break,
        };
        let mut batch = vec![first];
        if !coalesce.is_zero() {
            delay::sleep(coalesce).await;
        }
        while let Some(Some(r)) = stream.next().now_or_never() {
            match r {
                Ok(m) => batch.push(m),
                Err(_) => stats.undecodable_signals += 1,
            }
        }
        let repairs = apply_batch(&batch, &mut mirror, &mut naive, &mut stats);
        run_repairs(client, &mut mirror, repairs, &mut stats).await;
    }
    let maintenance_cost = meter.stop(client.counters());
    // Whatever arrived in the last moments is applied before comparing.
    let mut batch = Vec::new();
    while let Some(Some(Ok(m))) = stream.next().now_or_never() {
        batch.push(m);
    }
    let late_signals = batch.len();
    if !batch.is_empty() {
        let repairs = apply_batch(&batch, &mut mirror, &mut naive, &mut stats);
        run_repairs(client, &mut mirror, repairs, &mut stats).await;
    }

    delay::sleep(Duration::from_millis(300)).await;
    let meter = Meter::start(target.pid, client.counters());
    let walked = fetch::walk(client, ROOT_PATH, REPAIR_WINDOW, MIRROR_FIELDS, None).await;
    let reference_cost = meter.stop(client.counters());
    let reference = walked.tree;
    let fresh_items = fetch::get_items(client)
        .await
        .map_err(|e| format!("final GetItems: {e}"))?;
    let fresh = Tree::from_items(&target.app_bus, &fresh_items);
    let mut fresh_pruned = fresh.clone();
    let fresh_pruned_count = fresh_pruned.prune_unreachable();

    let mirror_diff = tree::diff(&reference, &mirror.tree, Compared::ALL);
    let drift = field_drift(&reference, &mirror.tree, &stats);
    // Each object counts once per kind of field it differs in, as in the
    // diff's total.
    let unexplained = mirror_diff.total()
        - drift.attributes.objects_without_signal
        - drift.states.objects_without_signal;
    // Both trees in full, for working out why a mirror went wrong.
    if mirror_diff.total() > 0
        && let Some(path) = args.0.get("dump")
    {
        write_json(
            path,
            &json!({"mirror": mirror.tree, "walk": reference, "signals": stats.signal_log}),
        )?;
    }
    write_json(
        out,
        &json!({
            "schema": "basedreader-bench/mirror/2",
            "title": title,
            "run_index": run_index,
            "seed": args.num("seed", 0)?,
            "interval_ms": args.num("interval-ms", 0)?,
            "unix_time": unix_time(),
            "wait": crate::wait_info(args),
            "target": info,
            "transport_for_calls": client.transport,
            "events_over": "bus",
            "build": {
                "cost": build_cost,
                "objects": build_objects,
                "getitems_items": items.len(),
                "pruned_unreachable": pruned_at_build,
                "call_errors": build_errors,
                "attribute_priming_calls": attribute_priming_calls,
                "objects_with_empty_attributes": empty_attributes,
                "status": status_at_build,
            },
            "build_s": build_s,
            "watch_ms": watch.as_millis(),
            "coalesce_ms": coalesce.as_millis(),
            "maintenance": {
                "cost": maintenance_cost,
                "loop": stats,
                "late_signals": late_signals,
                "rules": mirror.stats,
                "pooled_items_never_admitted": mirror.pool_len(),
            },
            "reference_walk": {
                "cost": reference_cost,
                "objects": reference.len(),
                "call_errors": walked.errors,
                "status": status(&reference),
            },
            "mirror_vs_walk": mirror_diff,
            "mirror_drift": drift,
            "mirror_differences_unexplained": unexplained,
            "cache_signals_only_vs_walk": tree::diff(&reference, &naive.tree, Compared::ITEM),
            "fresh_getitems_vs_walk": tree::diff(&reference, &fresh, Compared::ITEM),
            "fresh_getitems_pruned_vs_walk": tree::diff(&reference, &fresh_pruned, Compared::ITEM),
            "fresh_getitems_pruned_count": fresh_pruned_count,
        }),
    )
}
