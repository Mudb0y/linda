//! A plain in-memory copy of one application's accessibility tree.

use std::collections::{BTreeMap, HashMap, HashSet};

use serde::Serialize;

use crate::consts::{ROOT_PATH, state_name};
use crate::decode::{CacheItem, TreeEntry};

/// A field is None when the method that built the tree does not fetch it, so
/// a comparison skips it rather than counting it as different.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Node {
    pub parent: Option<String>,
    pub children: Vec<String>,
    pub index_in_parent: i32,
    pub child_count: i32,
    pub role: Option<u32>,
    pub name: Option<String>,
    pub description: Option<String>,
    pub states: Option<[u32; 2]>,
    pub interfaces: Option<u32>,
    pub attributes: Option<Vec<(String, String)>>,
    pub text: Option<String>,
}

impl Node {
    pub fn from_item(it: &CacheItem, app_bus: &str) -> Self {
        Node {
            parent: (it.parent.bus == app_bus && !it.parent.is_null())
                .then(|| it.parent.path.clone()),
            children: Vec::new(),
            index_in_parent: it.index_in_parent,
            child_count: it.child_count,
            role: Some(it.role),
            name: Some(it.name.clone()),
            description: Some(it.description.clone()),
            states: Some(it.states),
            interfaces: Some(it.interfaces),
            attributes: None,
            text: None,
        }
    }
}

/// Keyed by object path within one application, whose unique bus name is
/// `bus`; references to other bus names are not followed.
#[derive(Debug, Clone, Default)]
pub struct Tree {
    pub bus: String,
    pub root: String,
    pub nodes: HashMap<String, Node>,
}

impl Tree {
    pub fn new(bus: &str, root: &str) -> Self {
        Self {
            bus: bus.to_owned(),
            root: root.to_owned(),
            nodes: HashMap::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.nodes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }

    /// Builds the tree from a `GetItems` reply. The reply is in hash-table
    /// order, so each parent's children are ordered by their index fields;
    /// children with index -1 go last, in reply order.
    pub fn from_items(bus: &str, items: &[CacheItem]) -> Self {
        let mut t = Tree::new(bus, ROOT_PATH);
        let mut by_parent: HashMap<&str, Vec<(i32, usize, &str)>> = HashMap::new();
        for (n, it) in items.iter().enumerate() {
            if it.object.bus != bus {
                continue;
            }
            let node = Node::from_item(it, bus);
            if node.parent.is_some() {
                let idx = if it.index_in_parent < 0 {
                    i32::MAX
                } else {
                    it.index_in_parent
                };
                by_parent.entry(it.parent.path.as_str()).or_default().push((
                    idx,
                    n,
                    it.object.path.as_str(),
                ));
            }
            t.nodes.insert(it.object.path.clone(), node);
        }
        for (parent, mut kids) in by_parent {
            kids.sort_unstable();
            if let Some(node) = t.nodes.get_mut(parent) {
                node.children = kids.into_iter().map(|(_, _, p)| p.to_owned()).collect();
            }
        }
        t
    }

    /// Builds the tree from `Collection.GetTree`, which lists objects depth
    /// first, so appearance order under a parent is child order.
    pub fn from_tree_entries(bus: &str, root: &str, entries: &[TreeEntry]) -> Self {
        let mut t = Tree::new(bus, root);
        for e in entries.iter().filter(|e| e.object.bus == bus) {
            let parent = e
                .parent
                .as_ref()
                .filter(|p| p.bus == bus && !p.is_null() && e.object.path != root)
                .map(|p| p.path.clone());
            t.nodes.insert(
                e.object.path.clone(),
                Node {
                    parent,
                    child_count: e.child_count.unwrap_or(-1),
                    name: e.name.clone(),
                    description: e.description.clone(),
                    attributes: e.attributes.clone(),
                    ..Node::default()
                },
            );
        }
        for e in entries.iter().filter(|e| e.object.bus == bus) {
            let Some(parent) = t.nodes[&e.object.path].parent.clone() else {
                continue;
            };
            if let Some(p) = t.nodes.get_mut(&parent) {
                p.children.push(e.object.path.clone());
            }
        }
        t.renumber_all();
        t
    }

    pub fn renumber(&mut self, parent: &str) {
        let Some(kids) = self.nodes.get(parent).map(|n| n.children.clone()) else {
            return;
        };
        for (i, k) in kids.iter().enumerate() {
            if let Some(n) = self.nodes.get_mut(k) {
                n.index_in_parent = i as i32;
            }
        }
    }

    pub fn renumber_all(&mut self) {
        let parents: Vec<String> = self.nodes.keys().cloned().collect();
        for p in parents {
            self.renumber(&p);
        }
    }

    /// Paths reachable from the root through each node's child list.
    pub fn reachable(&self) -> HashSet<&str> {
        let mut seen = HashSet::new();
        let mut stack = vec![self.root.as_str()];
        while let Some(p) = stack.pop() {
            let Some(n) = self.nodes.get(p) else { continue };
            if !seen.insert(p) {
                continue;
            }
            stack.extend(n.children.iter().map(String::as_str));
        }
        seen
    }

    /// Drops every node that cannot be reached from the root, which is how a
    /// `GetItems` reply is cleaned of removed objects and phantom text leaves.
    /// Returns how many were dropped.
    pub fn prune_unreachable(&mut self) -> usize {
        let keep: HashSet<String> = self.reachable().into_iter().map(str::to_owned).collect();
        let before = self.nodes.len();
        self.nodes.retain(|k, _| keep.contains(k));
        before - self.nodes.len()
    }

    /// Removes a node and its whole subtree, and unlinks it from its parent.
    /// Returns how many nodes went.
    pub fn remove_subtree(&mut self, path: &str) -> usize {
        let Some(node) = self.nodes.get(path) else {
            return 0;
        };
        if let Some(parent) = node.parent.clone()
            && let Some(p) = self.nodes.get_mut(&parent)
        {
            p.children.retain(|c| c != path);
            self.renumber(&parent);
        }
        let mut removed = 0;
        let mut stack = vec![path.to_owned()];
        while let Some(p) = stack.pop() {
            if let Some(n) = self.nodes.remove(&p) {
                removed += 1;
                stack.extend(n.children);
            }
        }
        removed
    }

    pub fn same_objects(&self, other: &Tree) -> ObjectSetComparison {
        let missing = self
            .nodes
            .keys()
            .filter(|k| !other.nodes.contains_key(*k))
            .count();
        let extra = other
            .nodes
            .keys()
            .filter(|k| !self.nodes.contains_key(*k))
            .count();
        ObjectSetComparison {
            equal: missing == 0 && extra == 0,
            missing,
            extra,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct ObjectSetComparison {
    pub equal: bool,
    /// In the reference but not in the other.
    pub missing: usize,
    /// In the other but not in the reference.
    pub extra: usize,
}

/// Differences of `other` from `reference`, by kind.
#[derive(Debug, Clone, Default, Serialize)]
pub struct TreeDiff {
    pub reference_objects: usize,
    pub other_objects: usize,
    pub missing: usize,
    pub extra: usize,
    pub parent_wrong: usize,
    pub child_order_wrong: usize,
    pub index_wrong: usize,
    pub role_wrong: usize,
    pub name_wrong: usize,
    pub description_wrong: usize,
    pub states_wrong: usize,
    pub interfaces_wrong: usize,
    pub attributes_wrong: usize,
    pub text_wrong: usize,
    /// For objects whose states differ, how often each state bit differed.
    pub state_bits_wrong: BTreeMap<String, usize>,
    /// Up to five object paths for each kind of difference.
    pub samples: BTreeMap<String, Vec<String>>,
}

impl TreeDiff {
    pub fn total(&self) -> usize {
        self.missing
            + self.extra
            + self.parent_wrong
            + self.child_order_wrong
            + self.index_wrong
            + self.role_wrong
            + self.name_wrong
            + self.description_wrong
            + self.states_wrong
            + self.interfaces_wrong
            + self.attributes_wrong
            + self.text_wrong
    }

    fn note(&mut self, kind: &str, path: &str) {
        let s = self.samples.entry(kind.to_owned()).or_default();
        if s.len() < 5 {
            s.push(path.to_owned());
        }
    }
}

fn differs<T: PartialEq>(a: &Option<T>, b: &Option<T>) -> bool {
    matches!((a, b), (Some(x), Some(y)) if x != y)
}

pub fn diff(reference: &Tree, other: &Tree) -> TreeDiff {
    let mut d = TreeDiff {
        reference_objects: reference.len(),
        other_objects: other.len(),
        ..TreeDiff::default()
    };
    let mut paths: Vec<&String> = reference.nodes.keys().collect();
    paths.sort();
    for path in paths {
        let r = &reference.nodes[path];
        let Some(o) = other.nodes.get(path) else {
            d.missing += 1;
            d.note("missing", path);
            continue;
        };
        if r.parent != o.parent {
            d.parent_wrong += 1;
            d.note("parent_wrong", path);
        }
        if r.children != o.children {
            d.child_order_wrong += 1;
            d.note("child_order_wrong", path);
        }
        if let Some(parent) = &r.parent
            && let Some(pos) = reference.nodes[parent]
                .children
                .iter()
                .position(|c| c == path)
            && o.index_in_parent != pos as i32
        {
            d.index_wrong += 1;
            d.note("index_wrong", path);
        }
        if differs(&r.role, &o.role) {
            d.role_wrong += 1;
            d.note("role_wrong", path);
        }
        if differs(&r.name, &o.name) {
            d.name_wrong += 1;
            d.note("name_wrong", path);
        }
        if differs(&r.description, &o.description) {
            d.description_wrong += 1;
            d.note("description_wrong", path);
        }
        if let (Some(a), Some(b)) = (r.states, o.states)
            && a != b
        {
            d.states_wrong += 1;
            d.note("states_wrong", path);
            for bit in 0..64 {
                if (a[bit / 32] ^ b[bit / 32]) & (1 << (bit % 32)) != 0 {
                    *d.state_bits_wrong
                        .entry(state_name(bit as u32).to_owned())
                        .or_default() += 1;
                }
            }
        }
        if differs(&r.interfaces, &o.interfaces) {
            d.interfaces_wrong += 1;
            d.note("interfaces_wrong", path);
        }
        if differs(&r.attributes, &o.attributes) {
            d.attributes_wrong += 1;
            d.note("attributes_wrong", path);
        }
        if differs(&r.text, &o.text) {
            d.text_wrong += 1;
            d.note("text_wrong", path);
        }
    }
    let mut extra: Vec<&String> = other
        .nodes
        .keys()
        .filter(|k| !reference.nodes.contains_key(*k))
        .collect();
    extra.sort();
    d.extra = extra.len();
    for p in extra.into_iter().take(5) {
        d.note("extra", p);
    }
    d
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::decode::ObjRef;

    const APP: &str = ":1.7";

    fn r(path: &str) -> ObjRef {
        ObjRef {
            bus: APP.into(),
            path: path.into(),
        }
    }

    fn item(path: &str, parent: &str, index: i32) -> CacheItem {
        CacheItem {
            object: r(path),
            app: r(ROOT_PATH),
            parent: r(parent),
            index_in_parent: index,
            child_count: 0,
            interfaces: 0,
            name: path.into(),
            role: 1,
            description: String::new(),
            states: [0, 0],
        }
    }

    fn sample_items() -> Vec<CacheItem> {
        let mut root = item(ROOT_PATH, ROOT_PATH, -1);
        root.parent.bus = ":1.0".into();
        vec![
            item("/c", "/a", 1),
            root,
            item("/b", "/a", 0),
            item("/a", ROOT_PATH, 0),
            item("/t", "/a", -1),
        ]
    }

    #[test]
    fn items_are_ordered_by_index_not_reply_order() {
        let t = Tree::from_items(APP, &sample_items());
        assert_eq!(t.len(), 5);
        assert_eq!(t.nodes[ROOT_PATH].parent, None);
        assert_eq!(t.nodes[ROOT_PATH].children, vec!["/a"]);
        assert_eq!(t.nodes["/a"].children, vec!["/b", "/c", "/t"]);
        assert_eq!(t.nodes["/c"].parent.as_deref(), Some("/a"));
    }

    #[test]
    fn unreachable_items_are_pruned() {
        let mut items = sample_items();
        items.push(item("/orphan", "/gone", 0));
        let mut t = Tree::from_items(APP, &items);
        assert_eq!(t.prune_unreachable(), 1);
        assert!(!t.nodes.contains_key("/orphan"));
    }

    #[test]
    fn removing_a_subtree_renumbers_siblings() {
        let mut t = Tree::from_items(APP, &sample_items());
        t.nodes.get_mut("/b").unwrap().children.push("/c".into());
        assert_eq!(t.remove_subtree("/b"), 2);
        assert!(!t.nodes.contains_key("/c"));
        assert_eq!(t.nodes["/a"].children, vec!["/c", "/t"]);
        assert_eq!(t.nodes["/t"].index_in_parent, 1);
    }

    #[test]
    fn diff_counts_each_kind() {
        let reference = Tree::from_items(APP, &sample_items());
        let mut other = reference.clone();
        other.remove_subtree("/t");
        other.nodes.get_mut("/b").unwrap().name = Some("renamed".into());
        other.nodes.get_mut("/c").unwrap().states = Some([1 << 12, 0]);
        other.nodes.get_mut("/c").unwrap().text = Some("x".into());
        other.nodes.insert("/stale".into(), Node::default());
        let d = diff(&reference, &other);
        assert_eq!(d.missing, 1);
        assert_eq!(d.extra, 1);
        assert_eq!(d.name_wrong, 1);
        assert_eq!(d.states_wrong, 1);
        assert_eq!(d.state_bits_wrong.get("focused"), Some(&1));
        assert_eq!(d.child_order_wrong, 1);
        // Text is not compared where the reference never fetched it.
        assert_eq!(d.text_wrong, 0);
        assert!(!reference.same_objects(&other).equal);
        assert!(reference.same_objects(&reference).equal);
    }

    #[test]
    fn get_tree_entries_keep_depth_first_order() {
        let e = |p: &str, parent: Option<&str>| TreeEntry {
            object: r(p),
            parent: parent.map(r),
            name: Some(p.into()),
            description: None,
            child_count: Some(0),
            attributes: None,
        };
        let entries = vec![
            e(ROOT_PATH, None),
            e("/a", Some(ROOT_PATH)),
            e("/a1", Some("/a")),
            e("/b", Some(ROOT_PATH)),
        ];
        let t = Tree::from_tree_entries(APP, ROOT_PATH, &entries);
        assert_eq!(t.nodes[ROOT_PATH].children, vec!["/a", "/b"]);
        assert_eq!(t.nodes["/b"].index_in_parent, 1);
        assert_eq!(t.reachable().len(), 4);
    }
}
