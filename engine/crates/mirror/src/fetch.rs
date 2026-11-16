//! Fetching a tree: per-object walks with a window of calls in flight, the
//! bridge's one-call `Cache.GetItems`, and its undocumented
//! `Collection.GetTree`.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::time::{Duration, Instant};

use futures_util::future::{Either, LocalBoxFuture, select};
use futures_util::stream::{FuturesUnordered, StreamExt};
use zbus::Message;

use crate::Error;
use crate::client::{CallFailure, Client, FailureKind};
use crate::consts::{
    CACHE_PATH, IFACE_ACCESSIBLE, IFACE_CACHE, IFACE_COLLECTION, IFACE_PROPERTIES, IFACE_TEXT,
    INTERFACE_TEXT_BIT,
};
use crate::decode::{self, CacheItem, TreeEntry};
use crate::time;
use crate::tree::{Node, Tree};

/// What a virtual buffer needs from each object, one call each.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Field {
    Children,
    Role,
    States,
    Name,
    Description,
    Interfaces,
    Attributes,
    Text,
}

impl Field {
    pub fn label(self) -> &'static str {
        match self {
            Field::Children => "GetChildren",
            Field::Role => "GetRole",
            Field::States => "GetState",
            Field::Name => "Get(Name)",
            Field::Description => "Get(Description)",
            Field::Interfaces => "GetInterfaces",
            Field::Attributes => "GetAttributes",
            Field::Text => "Text.GetText",
        }
    }

    /// The interface and member of the call that fetches the field.
    pub fn method(self) -> (&'static str, &'static str) {
        match self {
            Field::Children => (IFACE_ACCESSIBLE, "GetChildren"),
            Field::Role => (IFACE_ACCESSIBLE, "GetRole"),
            Field::States => (IFACE_ACCESSIBLE, "GetState"),
            Field::Name | Field::Description => (IFACE_PROPERTIES, "Get"),
            Field::Interfaces => (IFACE_ACCESSIBLE, "GetInterfaces"),
            Field::Attributes => (IFACE_ACCESSIBLE, "GetAttributes"),
            Field::Text => (IFACE_TEXT, "GetText"),
        }
    }

    /// What the call's reply carries.
    pub fn reply_signature(self) -> &'static str {
        match self {
            Field::Children => "a(so)",
            Field::Role => "u",
            Field::States => "au",
            Field::Name | Field::Description => "v holding s",
            Field::Interfaces => "as",
            Field::Attributes => "a{ss}",
            Field::Text => "s",
        }
    }
}

pub type Call = (String, Field);

fn issue(
    client: &Client,
    id: u64,
    path: String,
    field: Field,
) -> LocalBoxFuture<'_, (u64, zbus::Result<Message>)> {
    Box::pin(async move {
        let p = path.as_str();
        let (iface, member) = field.method();
        let r = match field {
            Field::Name => {
                client
                    .call(p, iface, member, &(IFACE_ACCESSIBLE, "Name"))
                    .await
            }
            Field::Description => {
                client
                    .call(p, iface, member, &(IFACE_ACCESSIBLE, "Description"))
                    .await
            }
            Field::Text => client.call(p, iface, member, &(0i32, -1i32)).await,
            _ => client.call(p, iface, member, &()).await,
        };
        (id, r)
    })
}

/// Keeps up to `window` calls in flight until the queue and the window are
/// both empty, or until `deadline`. `on_reply` may queue further calls. A
/// window of 1 is a plain sequential client: one call, wait for its reply,
/// then the next. Returns the calls queued or still in flight at the
/// deadline, which are given up.
pub async fn pipeline<F>(
    client: &Client,
    window: usize,
    initial: impl IntoIterator<Item = Call>,
    deadline: Option<Instant>,
    mut on_reply: F,
) -> Vec<Call>
where
    F: FnMut(String, Field, zbus::Result<Message>, &mut VecDeque<Call>),
{
    let window = window.max(1);
    let mut queue: VecDeque<Call> = initial.into_iter().collect();
    let mut inflight = FuturesUnordered::new();
    let mut pending: HashMap<u64, Call> = HashMap::new();
    let mut next_id = 0u64;
    // Without a timer the deadline still holds at the next reply, and every
    // call is bounded by the method timeout.
    let mut timer = deadline.and_then(|at| time::sleep_until(at).ok());
    loop {
        if deadline.is_some_and(|at| Instant::now() >= at) {
            break;
        }
        while inflight.len() < window {
            let Some((path, field)) = queue.pop_front() else {
                break;
            };
            pending.insert(next_id, (path.clone(), field));
            inflight.push(issue(client, next_id, path, field));
            next_id += 1;
        }
        let reply = match timer.as_mut() {
            Some(t) => match select(inflight.next(), t).await {
                Either::Left((r, _)) => r,
                Either::Right(_) => break,
            },
            None => inflight.next().await,
        };
        let Some((id, r)) = reply else {
            break;
        };
        if let Some((path, field)) = pending.remove(&id) {
            on_reply(path, field, r, &mut queue);
        }
    }
    let mut left: Vec<Call> = pending.into_values().collect();
    left.extend(queue);
    left
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WalkFields {
    /// Role, states, name, description and interfaces. Without them a walk
    /// fetches structure only.
    pub data: bool,
    pub attributes: bool,
    pub text: bool,
}

impl WalkFields {
    pub const ALL: WalkFields = WalkFields {
        data: true,
        attributes: true,
        text: true,
    };
    /// What `GetItems` returns, for comparing the two with equal data.
    pub const ITEM: WalkFields = WalkFields {
        data: true,
        attributes: false,
        text: false,
    };
    pub const STRUCTURE: WalkFields = WalkFields {
        data: false,
        attributes: false,
        text: false,
    };

    pub fn asks_for(self, f: Field) -> bool {
        match f {
            Field::Children => true,
            Field::Role | Field::States | Field::Name | Field::Description | Field::Interfaces => {
                self.data
            }
            Field::Attributes => self.attributes,
            Field::Text => self.text,
        }
    }
}

/// Bounds on one walk, so that an application that invents objects without
/// end, or stops answering, cannot keep it going for ever. Both are reported
/// when reached, never silently.
#[derive(Debug, Clone, Copy)]
pub struct WalkLimits {
    /// The most objects a walk takes in. A child list that would take it
    /// past the limit is not taken.
    pub max_objects: usize,
    /// Calls not answered by then are given up.
    pub max_time: Duration,
}

/// How many failed calls a walk keeps in full; the rest are only counted.
pub const MAX_FAILURES_KEPT: usize = 64;

#[derive(Debug)]
pub struct WalkResult {
    pub tree: Tree,
    /// What the walk asked each object for.
    pub fields: WalkFields,
    /// Failed calls by method.
    pub errors: BTreeMap<&'static str, usize>,
    /// The first failed calls, in full.
    pub failures: Vec<CallFailure>,
    pub failures_not_kept: usize,
    /// For each object, the fields the walk did not get, and why. Such a
    /// field is unknown, not empty: an object whose `GetChildren` timed out
    /// may well have children.
    pub failed: HashMap<String, BTreeMap<Field, FailureKind>>,
    /// The walk reached its object limit or ran out of time.
    pub truncated: bool,
    /// Children owned by another bus name, such as an embedded application.
    pub foreign_children: usize,
}

impl WalkResult {
    fn new(bus: &str, root: &str, fields: WalkFields) -> Self {
        Self::from_tree(Tree::new(bus, root), fields)
    }

    /// A walk that got every field in `fields` for every object of `tree`.
    pub fn from_tree(tree: Tree, fields: WalkFields) -> Self {
        WalkResult {
            tree,
            fields,
            errors: BTreeMap::new(),
            failures: Vec::new(),
            failures_not_kept: 0,
            failed: HashMap::new(),
            truncated: false,
            foreign_children: 0,
        }
    }

    pub fn failure(&self, path: &str, field: Field) -> Option<&FailureKind> {
        self.failed.get(path).and_then(|f| f.get(&field))
    }
}

/// Cache items a walk may take objects' data from instead of asking for it.
pub trait Known {
    fn item(&self, path: &str) -> Option<&CacheItem>;
}

impl Known for HashMap<String, CacheItem> {
    fn item(&self, path: &str) -> Option<&CacheItem> {
        self.get(path)
    }
}

struct Failures<'a> {
    client: &'a Client,
    errors: &'a mut BTreeMap<&'static str, usize>,
    failures: &'a mut Vec<CallFailure>,
    not_kept: &'a mut usize,
    failed: &'a mut HashMap<String, BTreeMap<Field, FailureKind>>,
}

impl Failures<'_> {
    fn record(&mut self, path: &str, field: Field, kind: FailureKind) {
        if !matches!(kind, FailureKind::NotMade { .. } | FailureKind::OverLimit) {
            *self.errors.entry(field.label()).or_default() += 1;
            if self.failures.len() < MAX_FAILURES_KEPT {
                let (iface, member) = field.method();
                self.failures
                    .push(self.client.failure(path, iface, member, kind.clone()));
            } else {
                *self.not_kept += 1;
            }
        }
        self.failed
            .entry(path.to_owned())
            .or_default()
            .insert(field, kind);
    }
}

fn data_calls(path: &str, fields: WalkFields) -> impl Iterator<Item = Call> + '_ {
    let mut f = Vec::with_capacity(6);
    if fields.data {
        f.extend([
            Field::Role,
            Field::States,
            Field::Name,
            Field::Description,
            Field::Interfaces,
        ]);
    }
    if fields.attributes {
        f.push(Field::Attributes);
    }
    f.into_iter().map(move |f| (path.to_owned(), f))
}

/// Walks the subtree under `root` through `GetChildren`, fetching each
/// object's data with one call per field. Where `known` holds a cache item
/// for an object, its data comes from there and only the calls the item
/// cannot answer are made: children unless the item says there are none,
/// attributes, and text. Every object is taken in once, so the tree returned
/// has no cycles whatever the application answers.
pub async fn walk(
    client: &Client,
    root: &str,
    window: usize,
    fields: WalkFields,
    known: Option<&dyn Known>,
    limits: WalkLimits,
) -> WalkResult {
    let deadline = Instant::now().checked_add(limits.max_time);
    let mut res = WalkResult::new(&client.app_bus, root, fields);
    let app_bus = client.app_bus.clone();
    let start = |path: &str, tree: &mut Tree, initial: &mut Vec<Call>| {
        let item = known.and_then(|k| k.item(path));
        let node = tree.nodes.entry(path.to_owned()).or_default();
        match item {
            Some(it) => {
                let parent = node.parent.take();
                let index = node.index_in_parent;
                *node = Node::from_item(it, &app_bus);
                node.parent = parent;
                node.index_in_parent = index;
                if it.child_count != 0 {
                    initial.push((path.to_owned(), Field::Children));
                }
                if fields.attributes {
                    initial.push((path.to_owned(), Field::Attributes));
                }
                if fields.text && it.interfaces & INTERFACE_TEXT_BIT != 0 {
                    initial.push((path.to_owned(), Field::Text));
                }
            }
            None => {
                initial.push((path.to_owned(), Field::Children));
                initial.extend(data_calls(path, fields));
            }
        }
    };
    let mut initial = Vec::new();
    res.tree.nodes.insert(
        root.to_owned(),
        Node {
            index_in_parent: -1,
            ..Node::default()
        },
    );
    start(root, &mut res.tree, &mut initial);
    let tree = &mut res.tree;
    let foreign = &mut res.foreign_children;
    let truncated = &mut res.truncated;
    let mut fails = Failures {
        client,
        errors: &mut res.errors,
        failures: &mut res.failures,
        not_kept: &mut res.failures_not_kept,
        failed: &mut res.failed,
    };
    // The text call is only made once the interfaces are known.
    let text_unknown = FailureKind::NotMade {
        reason: "the interfaces are unknown",
    };
    let left = pipeline(
        client,
        window,
        initial,
        deadline,
        |path, field, r, queue| {
            let msg = match r {
                Ok(m) => m,
                Err(e) => {
                    fails.record(&path, field, FailureKind::of(&e));
                    if field == Field::Interfaces && fields.text {
                        fails.record(&path, Field::Text, text_unknown.clone());
                    }
                    return;
                }
            };
            let decoded: zbus::Result<()> = (|| {
                match field {
                    Field::Children => {
                        let kids = decode::children(&msg)?;
                        let mut own = Vec::with_capacity(kids.len());
                        let mut here = HashSet::with_capacity(kids.len());
                        for k in kids {
                            if k.is_null() {
                                continue;
                            }
                            if k.bus != app_bus {
                                *foreign += 1;
                                continue;
                            }
                            if tree.nodes.contains_key(&k.path) || !here.insert(k.path.clone()) {
                                continue;
                            }
                            own.push(k.path);
                        }
                        if tree.nodes.len() + own.len() > limits.max_objects {
                            *truncated = true;
                            fails.record(&path, Field::Children, FailureKind::OverLimit);
                            return Ok(());
                        }
                        for (i, k) in own.iter().enumerate() {
                            tree.nodes.insert(
                                k.clone(),
                                Node {
                                    parent: Some(path.clone()),
                                    index_in_parent: i as i32,
                                    ..Node::default()
                                },
                            );
                            let mut more = Vec::new();
                            start(k, tree, &mut more);
                            queue.extend(more);
                        }
                        if let Some(n) = tree.nodes.get_mut(&path) {
                            n.child_count = own.len() as i32;
                            n.children = own;
                        }
                    }
                    Field::Role => node(tree, &path).role = Some(decode::role(&msg)?),
                    Field::States => node(tree, &path).states = Some(decode::states(&msg)?),
                    Field::Name => node(tree, &path).name = Some(decode::string_property(&msg)?),
                    Field::Description => {
                        node(tree, &path).description = Some(decode::string_property(&msg)?)
                    }
                    Field::Interfaces => {
                        let bits = decode::interfaces(&msg)?;
                        node(tree, &path).interfaces = Some(bits);
                        if fields.text && bits & INTERFACE_TEXT_BIT != 0 {
                            queue.push_back((path.clone(), Field::Text));
                        }
                    }
                    Field::Attributes => {
                        node(tree, &path).attributes = Some(decode::attributes(&msg)?)
                    }
                    Field::Text => node(tree, &path).text = Some(decode::string(&msg)?),
                }
                Ok(())
            })();
            if decoded.is_err() {
                fails.record(
                    &path,
                    field,
                    FailureKind::undecodable(&msg, field.reply_signature()),
                );
                if field == Field::Interfaces && fields.text {
                    fails.record(&path, Field::Text, text_unknown.clone());
                }
            }
        },
    )
    .await;
    if !left.is_empty() {
        *truncated = true;
    }
    let out_of_time = FailureKind::NotMade {
        reason: "the walk ran out of time",
    };
    for (path, field) in left {
        fails.record(&path, field, out_of_time.clone());
        if field == Field::Interfaces && fields.text {
            fails.record(&path, Field::Text, out_of_time.clone());
        }
    }
    res
}

fn node<'t>(tree: &'t mut Tree, path: &str) -> &'t mut Node {
    tree.nodes.entry(path.to_owned()).or_default()
}

/// What [`supplement`] did not get.
#[derive(Debug, Default)]
pub struct SupplementResult {
    /// Failed calls by method.
    pub errors: BTreeMap<&'static str, usize>,
    /// The first failed calls, in full.
    pub failures: Vec<CallFailure>,
    pub failures_not_kept: usize,
    /// For each object, the fields not got, and why.
    pub failed: HashMap<String, BTreeMap<Field, FailureKind>>,
}

/// Fetches attributes and text for every object of a tree built from
/// `GetItems`, which carries neither.
pub async fn supplement(
    client: &Client,
    tree: &mut Tree,
    window: usize,
    fields: WalkFields,
) -> SupplementResult {
    let mut calls = Vec::new();
    let mut paths: Vec<&String> = tree.nodes.keys().collect();
    paths.sort();
    for p in paths {
        if fields.attributes {
            calls.push((p.clone(), Field::Attributes));
        }
        if fields.text
            && tree.nodes[p]
                .interfaces
                .is_some_and(|i| i & INTERFACE_TEXT_BIT != 0)
        {
            calls.push((p.clone(), Field::Text));
        }
    }
    let mut res = SupplementResult::default();
    let mut fails = Failures {
        client,
        errors: &mut res.errors,
        failures: &mut res.failures,
        not_kept: &mut res.failures_not_kept,
        failed: &mut res.failed,
    };
    pipeline(client, window, calls, None, |path, field, r, _| {
        let msg = match r {
            Ok(m) => m,
            Err(e) => return fails.record(&path, field, FailureKind::of(&e)),
        };
        let decoded = (|| {
            match field {
                Field::Attributes => node(tree, &path).attributes = Some(decode::attributes(&msg)?),
                Field::Text => node(tree, &path).text = Some(decode::string(&msg)?),
                _ => {}
            }
            Ok::<_, zbus::Error>(())
        })();
        if decoded.is_err() {
            fails.record(
                &path,
                field,
                FailureKind::undecodable(&msg, field.reply_signature()),
            );
        }
    })
    .await;
    res
}

pub async fn get_items(client: &Client) -> Result<Vec<CacheItem>, Error> {
    let msg = client
        .request(CACHE_PATH, IFACE_CACHE, "GetItems", &())
        .await?;
    client.decode(
        &msg,
        CACHE_PATH,
        IFACE_CACHE,
        "GetItems",
        "a((so)(so)(so)iiassusau)",
        decode::items,
    )
}

/// The match rule `(aiia{ss}iaiiasib)` that matches everything: an empty set
/// with match type ALL for each criterion. The bridge parses the rule for
/// GetTree but never applies it; a match type of 0 would fail every object.
type MatchRule<'a> = (
    Vec<i32>,
    i32,
    HashMap<&'a str, &'a str>,
    i32,
    Vec<i32>,
    i32,
    Vec<&'a str>,
    i32,
    bool,
);

fn match_all<'a>() -> MatchRule<'a> {
    (
        vec![0, 0],
        1,
        HashMap::new(),
        1,
        vec![0; 5],
        1,
        Vec::new(),
        1,
        false,
    )
}

/// `Collection.GetTree` on `root`, with the property names to return; an
/// empty list returns every property of every interface.
pub async fn get_tree(
    client: &Client,
    root: &str,
    properties: &[&str],
) -> Result<Vec<TreeEntry>, Error> {
    let msg = client
        .request(
            root,
            IFACE_COLLECTION,
            "GetTree",
            &(match_all(), properties.to_vec()),
        )
        .await?;
    client.decode(
        &msg,
        root,
        IFACE_COLLECTION,
        "GetTree",
        "a((so)a{sv})",
        decode::tree,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn get_tree_arguments_have_the_bridge_signature() {
        let msg = Message::method_call("/x", "GetTree")
            .build(&(match_all(), vec!["Name"]))
            .unwrap();
        // zbus shows a body of several arguments in outer parentheses.
        assert_eq!(msg.body().signature().to_string(), "((aiia{ss}iaiiasib)as)");
    }
}
