//! Part two: build a mirror from `GetItems`, keep it current from signals
//! while the page mutates on its schedule, then compare it with a fresh walk.

use std::collections::{HashSet, VecDeque};
use std::time::{Duration, Instant};

use futures_util::FutureExt;
use futures_util::future::{Either, select};
use futures_util::stream::StreamExt;
use mirror::client::Client;
use mirror::consts::{IFACE_ACCESSIBLE, IFACE_CACHE, IFACE_TEXT, ROLE_HEADING, ROOT_PATH};
use mirror::decode;
use mirror::fetch::{self, WalkFields};
use mirror::maintain::{Mirror, NaiveMirror, Repair};
use mirror::tree::{self, Tree};
use mirror::{app, discovery};
use serde::Serialize;
use serde_json::json;
use zbus::message::Type;
use zbus::{MatchRule, Message, MessageStream};

use crate::delay::Delay;
use crate::procstat::Meter;
use crate::{Args, delay, unix_time, write_json};

const STATUS_PREFIX: &str = "Mutations ";

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
}

async fn run_repairs(
    client: &Client,
    mirror: &mut Mirror,
    repairs: Vec<Repair>,
    stats: &mut LoopStats,
) {
    let mut seen = HashSet::new();
    let mut queue: VecDeque<Repair> = repairs
        .into_iter()
        .filter(|r| seen.insert(r.clone()))
        .collect();
    let t = Instant::now();
    while let Some(r) = queue.pop_front() {
        let path = match &r {
            Repair::RefetchChildren(p) | Repair::FetchSubtree(p) | Repair::RefetchText(p) => p,
        };
        if !mirror.tree.nodes.contains_key(path) {
            stats.repairs_skipped_object_gone += 1;
            continue;
        }
        stats.repairs_run += 1;
        match &r {
            Repair::RefetchChildren(p) => {
                match client.call(p, IFACE_ACCESSIBLE, "GetChildren", &()).await {
                    Ok(msg) => {
                        if let Ok(live) = decode::children(&msg) {
                            queue.extend(mirror.reconcile_children(p, &live));
                        }
                    }
                    Err(e) if !mirror::client::is_timeout(&e) => mirror.forget(p),
                    Err(_) => {}
                }
            }
            Repair::FetchSubtree(p) => {
                let fields = WalkFields {
                    data: true,
                    attributes: false,
                    text: true,
                };
                let res = fetch::walk(client, p, 64, fields, Some(mirror.pool())).await;
                if res.children_failed.iter().any(|f| f == p) {
                    stats.subtree_roots_gone += 1;
                    mirror.forget(p);
                } else {
                    stats.objects_fetched_by_repairs += res.tree.len();
                    mirror.admit_subtree(res.tree);
                }
            }
            Repair::RefetchText(p) => {
                if let Ok(msg) = client.call(p, IFACE_TEXT, "GetText", &(0i32, -1i32)).await
                    && let Ok(text) = decode::string(&msg)
                {
                    mirror.set_text(p, text);
                }
            }
        }
    }
    stats.repair_wall_s += t.elapsed().as_secs_f64();
}

fn apply_batch(
    batch: &[Message],
    mirror: &mut Mirror,
    naive: &mut NaiveMirror,
    stats: &mut LoopStats,
) -> Vec<Repair> {
    stats.batches += 1;
    stats.largest_batch = stats.largest_batch.max(batch.len());
    stats.signals += batch.len();
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
    let items = app::get_items_once_active(client, Duration::from_secs(10))
        .await
        .map_err(crate::err)?;
    let mut tree = Tree::from_items(&target.app_bus, &items);
    let naive_tree = tree.clone();
    let pruned_at_build = tree.prune_unreachable();
    let text_fields = WalkFields {
        data: false,
        attributes: false,
        text: true,
    };
    let build_errors = fetch::supplement(client, &mut tree, 64, text_fields).await;
    let build_cost = meter.stop(client.counters());
    let status_at_build = status(&tree);
    let build_objects = tree.len();
    let mut mirror = Mirror::new(tree);
    let mut naive = NaiveMirror { tree: naive_tree };
    let build_s = t_build.elapsed().as_secs_f64();
    let watch_left = watch.saturating_sub(t_start.elapsed());

    let mut stats = LoopStats::default();
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
    let reference_fields = WalkFields {
        data: true,
        attributes: false,
        text: true,
    };
    let meter = Meter::start(target.pid, client.counters());
    let walked = fetch::walk(client, ROOT_PATH, 64, reference_fields, None).await;
    let reference_cost = meter.stop(client.counters());
    let reference = walked.tree;
    let fresh_items = fetch::get_items(client)
        .await
        .map_err(|e| format!("final GetItems: {e}"))?;
    let fresh = Tree::from_items(&target.app_bus, &fresh_items);
    let mut fresh_pruned = fresh.clone();
    let fresh_pruned_count = fresh_pruned.prune_unreachable();

    write_json(
        out,
        &json!({
            "schema": "basedreader-bench/mirror/1",
            "title": title,
            "run_index": run_index,
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
                "text_fetch_errors": build_errors,
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
            "mirror_vs_walk": tree::diff(&reference, &mirror.tree),
            "cache_signals_only_vs_walk": tree::diff(&reference, &naive.tree),
            "fresh_getitems_vs_walk": tree::diff(&reference, &fresh),
            "fresh_getitems_pruned_vs_walk": tree::diff(&reference, &fresh_pruned),
            "fresh_getitems_pruned_count": fresh_pruned_count,
        }),
    )
}
