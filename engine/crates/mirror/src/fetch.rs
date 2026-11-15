//! Fetching a tree: per-object walks with a window of calls in flight, the
//! bridge's one-call `Cache.GetItems`, and its undocumented
//! `Collection.GetTree`.

use std::collections::{BTreeMap, HashMap, VecDeque};

use futures_util::future::LocalBoxFuture;
use futures_util::stream::{FuturesUnordered, StreamExt};
use zbus::Message;

use crate::client::Client;
use crate::consts::{
    CACHE_PATH, IFACE_ACCESSIBLE, IFACE_CACHE, IFACE_COLLECTION, IFACE_PROPERTIES, IFACE_TEXT,
    INTERFACE_TEXT_BIT,
};
use crate::decode::{self, CacheItem, TreeEntry};
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
}

pub type Call = (String, Field);

fn issue<'a>(
    client: &'a Client,
    path: String,
    field: Field,
) -> LocalBoxFuture<'a, (String, Field, zbus::Result<Message>)> {
    Box::pin(async move {
        let p = path.as_str();
        let r = match field {
            Field::Children => client.call(p, IFACE_ACCESSIBLE, "GetChildren", &()).await,
            Field::Role => client.call(p, IFACE_ACCESSIBLE, "GetRole", &()).await,
            Field::States => client.call(p, IFACE_ACCESSIBLE, "GetState", &()).await,
            Field::Interfaces => client.call(p, IFACE_ACCESSIBLE, "GetInterfaces", &()).await,
            Field::Attributes => client.call(p, IFACE_ACCESSIBLE, "GetAttributes", &()).await,
            Field::Name => {
                client
                    .call(p, IFACE_PROPERTIES, "Get", &(IFACE_ACCESSIBLE, "Name"))
                    .await
            }
            Field::Description => {
                client
                    .call(
                        p,
                        IFACE_PROPERTIES,
                        "Get",
                        &(IFACE_ACCESSIBLE, "Description"),
                    )
                    .await
            }
            Field::Text => client.call(p, IFACE_TEXT, "GetText", &(0i32, -1i32)).await,
        };
        (path, field, r)
    })
}

/// Keeps up to `window` calls in flight until the queue and the window are
/// both empty. `on_reply` may queue further calls. A window of 1 is a plain
/// sequential client: one call, wait for its reply, then the next.
pub async fn pipeline<F>(
    client: &Client,
    window: usize,
    initial: impl IntoIterator<Item = Call>,
    mut on_reply: F,
) where
    F: FnMut(String, Field, zbus::Result<Message>, &mut VecDeque<Call>),
{
    let window = window.max(1);
    let mut queue: VecDeque<Call> = initial.into_iter().collect();
    let mut inflight = FuturesUnordered::new();
    loop {
        while inflight.len() < window {
            let Some((path, field)) = queue.pop_front() else {
                break;
            };
            inflight.push(issue(client, path, field));
        }
        let Some((path, field, r)) = inflight.next().await else {
            break;
        };
        on_reply(path, field, r, &mut queue);
    }
}

#[derive(Debug, Clone, Copy)]
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
}

#[derive(Debug, Default)]
pub struct WalkResult {
    pub tree: Tree,
    /// Failed calls by method.
    pub errors: BTreeMap<&'static str, usize>,
    /// Objects whose `GetChildren` failed with an error reply, which for an
    /// object the walk was just told about means it is already gone.
    pub children_failed: Vec<String>,
    /// Children owned by another bus name, such as an embedded application.
    pub foreign_children: usize,
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
/// attributes, and text.
pub async fn walk(
    client: &Client,
    root: &str,
    window: usize,
    fields: WalkFields,
    known: Option<&HashMap<String, CacheItem>>,
) -> WalkResult {
    let mut res = WalkResult {
        tree: Tree::new(&client.app_bus, root),
        ..WalkResult::default()
    };
    let app_bus = client.app_bus.clone();
    let start = |path: &str, tree: &mut Tree, initial: &mut Vec<Call>| {
        let item = known.and_then(|k| k.get(path));
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
    let errors = &mut res.errors;
    let foreign = &mut res.foreign_children;
    let failed = &mut res.children_failed;
    pipeline(client, window, initial, |path, field, r, queue| {
        let msg = match r {
            Ok(m) => m,
            Err(e) => {
                *errors.entry(field.label()).or_default() += 1;
                if field == Field::Children && !crate::client::is_timeout(&e) {
                    failed.push(path);
                }
                return;
            }
        };
        let decoded: zbus::Result<()> = (|| {
            match field {
                Field::Children => {
                    let kids = decode::children(&msg)?;
                    let mut own = Vec::with_capacity(kids.len());
                    for k in kids {
                        if k.is_null() {
                            continue;
                        }
                        if k.bus != app_bus {
                            *foreign += 1;
                            continue;
                        }
                        if tree.nodes.contains_key(&k.path) {
                            continue;
                        }
                        tree.nodes.insert(
                            k.path.clone(),
                            Node {
                                parent: Some(path.clone()),
                                index_in_parent: own.len() as i32,
                                ..Node::default()
                            },
                        );
                        let mut more = Vec::new();
                        start(&k.path, tree, &mut more);
                        queue.extend(more);
                        own.push(k.path);
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
                Field::Attributes => node(tree, &path).attributes = Some(decode::attributes(&msg)?),
                Field::Text => node(tree, &path).text = Some(decode::string(&msg)?),
            }
            Ok(())
        })();
        if decoded.is_err() {
            *errors.entry(field.label()).or_default() += 1;
        }
    })
    .await;
    res
}

fn node<'t>(tree: &'t mut Tree, path: &str) -> &'t mut Node {
    tree.nodes.entry(path.to_owned()).or_default()
}

/// Fetches attributes and text for every object of a tree built from
/// `GetItems`, which carries neither.
pub async fn supplement(
    client: &Client,
    tree: &mut Tree,
    window: usize,
    fields: WalkFields,
) -> BTreeMap<&'static str, usize> {
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
    let mut errors = BTreeMap::new();
    pipeline(client, window, calls, |path, field, r, _| {
        let ok = r.and_then(|msg| {
            match field {
                Field::Attributes => node(tree, &path).attributes = Some(decode::attributes(&msg)?),
                Field::Text => node(tree, &path).text = Some(decode::string(&msg)?),
                _ => {}
            }
            Ok(())
        });
        if ok.is_err() {
            *errors.entry(field.label()).or_default() += 1;
        }
    })
    .await;
    errors
}

pub async fn get_items(client: &Client) -> zbus::Result<Vec<CacheItem>> {
    let msg = client
        .call(CACHE_PATH, IFACE_CACHE, "GetItems", &())
        .await?;
    decode::items(&msg)
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
) -> zbus::Result<Vec<TreeEntry>> {
    let msg = client
        .call(
            root,
            IFACE_COLLECTION,
            "GetTree",
            &(match_all(), properties.to_vec()),
        )
        .await?;
    decode::tree(&msg)
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
