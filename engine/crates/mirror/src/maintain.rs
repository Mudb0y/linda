//! Keeping a mirror current from Cache signals and events.
//!
//! The rules follow what Firefox 160 was measured to send: structure changes
//! arrive as `ChildrenChanged` on the parent, removals never produce
//! `RemoveAccessible` or `defunct`, `AddAccessible` arrives for every object
//! of an inserted subtree but also for text leaves no `GetChildren` lists,
//! and re-shown content comes back under new paths. So:
//!
//! - `ChildrenChanged` is the authority for structure. A removal drops the
//!   child's whole subtree; an addition is placed at detail1, or the parent's
//!   live child list is fetched again when detail1 does not fit.
//! - `AddAccessible` is a data source only: its item is pooled and used when
//!   a `ChildrenChanged` or a live child list admits the object.
//! - `RemoveAccessible` and `defunct` are removal hints.
//! - Siblings are renumbered after every structural change.
//!
//! Applying an event never makes a call. Whatever needs the application is
//! returned as a [`Repair`] for the caller to carry out and feed back.

use std::collections::{BTreeMap, HashMap};

use serde::Serialize;

use crate::consts::{INTERFACE_TEXT_BIT, STATE_DEFUNCT, set_state, state_from_name};
use crate::decode::{AnyData, CacheItem, Event, ObjRef};
use crate::tree::{Node, Tree};

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Repair {
    /// Fetch this parent's live child list and reconcile against it.
    RefetchChildren(String),
    /// Walk this newly admitted object's subtree.
    FetchSubtree(String),
    /// Fetch this object's text again.
    RefetchText(String),
}

impl Repair {
    pub fn kind(&self) -> &'static str {
        match self {
            Repair::RefetchChildren(_) => "refetch_children",
            Repair::FetchSubtree(_) => "fetch_subtree",
            Repair::RefetchText(_) => "refetch_text",
        }
    }
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct MaintenanceStats {
    /// Every signal seen, by `Interface.Member:minor-kind`.
    pub signals: BTreeMap<String, usize>,
    pub events_for_unknown_objects: usize,
    pub add_accessible_pooled: usize,
    pub add_accessible_updated_existing: usize,
    pub remove_hints_applied: usize,
    pub remove_hints_for_unknown_objects: usize,
    pub defunct_applied: usize,
    pub children_added_at_index: usize,
    pub children_added_with_pooled_data: usize,
    pub children_add_index_did_not_fit: usize,
    pub children_add_already_present: usize,
    pub children_removed: usize,
    pub objects_dropped_with_subtrees: usize,
    pub children_remove_for_unknown_child: usize,
    pub reconcile_added: usize,
    pub reconcile_removed: usize,
    pub reconcile_moved: usize,
    pub states_applied: usize,
    pub names_applied: usize,
    pub descriptions_applied: usize,
    pub roles_applied: usize,
    pub text_patched: usize,
    pub text_patch_refused: usize,
    pub repairs_requested: BTreeMap<String, usize>,
}

pub struct Mirror {
    pub tree: Tree,
    pool: HashMap<String, CacheItem>,
    pub stats: MaintenanceStats,
}

impl Mirror {
    pub fn new(tree: Tree) -> Self {
        Self {
            tree,
            pool: HashMap::new(),
            stats: MaintenanceStats::default(),
        }
    }

    pub fn pool(&self) -> &HashMap<String, CacheItem> {
        &self.pool
    }

    pub fn pool_len(&self) -> usize {
        self.pool.len()
    }

    fn count_signal(&mut self, key: String) {
        *self.stats.signals.entry(key).or_default() += 1;
    }

    fn request(&mut self, out: &mut Vec<Repair>, r: Repair) {
        *self
            .stats
            .repairs_requested
            .entry(r.kind().to_owned())
            .or_default() += 1;
        out.push(r);
    }

    pub fn apply_add_accessible(&mut self, item: CacheItem) {
        self.count_signal("Cache.AddAccessible".into());
        if item.object.bus != self.tree.bus {
            return;
        }
        if let Some(n) = self.tree.nodes.get_mut(&item.object.path) {
            n.role = Some(item.role);
            n.name = Some(item.name.clone());
            n.description = Some(item.description.clone());
            n.states = Some(item.states);
            n.interfaces = Some(item.interfaces);
            self.stats.add_accessible_updated_existing += 1;
        } else {
            self.stats.add_accessible_pooled += 1;
        }
        self.pool.insert(item.object.path.clone(), item);
    }

    pub fn apply_remove_accessible(&mut self, r: &ObjRef) {
        self.count_signal("Cache.RemoveAccessible".into());
        self.pool.remove(&r.path);
        if r.bus != self.tree.bus || !self.tree.nodes.contains_key(&r.path) {
            self.stats.remove_hints_for_unknown_objects += 1;
            return;
        }
        self.stats.remove_hints_applied += 1;
        self.drop_subtree(&r.path);
    }

    fn drop_subtree(&mut self, path: &str) {
        let n = self.tree.remove_subtree(path);
        self.stats.objects_dropped_with_subtrees += n;
    }

    /// A node for a newly admitted object, from its pooled item where there
    /// is one. Returns the repairs that complete it.
    fn admit(&mut self, path: &str, parent: &str, out: &mut Vec<Repair>) {
        let bus = self.tree.bus.clone();
        match self.pool.remove(path) {
            Some(item) => {
                self.stats.children_added_with_pooled_data += 1;
                let mut node = Node::from_item(&item, &bus);
                node.parent = Some(parent.to_owned());
                let needs_children = item.child_count != 0;
                let needs_text = item.interfaces & INTERFACE_TEXT_BIT != 0;
                self.tree.nodes.insert(path.to_owned(), node);
                // The pool goes back so that the subtree walk can use it.
                self.pool.insert(path.to_owned(), item);
                if needs_children {
                    self.request(out, Repair::FetchSubtree(path.to_owned()));
                } else if needs_text {
                    self.request(out, Repair::RefetchText(path.to_owned()));
                }
            }
            None => {
                self.tree.nodes.insert(
                    path.to_owned(),
                    Node {
                        parent: Some(parent.to_owned()),
                        ..Node::default()
                    },
                );
                self.request(out, Repair::FetchSubtree(path.to_owned()));
            }
        }
    }

    pub fn apply_event(&mut self, e: &Event) -> Vec<Repair> {
        self.count_signal(format!("{}.{}:{}", e.class, e.member, e.minor_kind()));
        let mut out = Vec::new();
        if e.sender != self.tree.bus {
            return out;
        }
        if !self.tree.nodes.contains_key(&e.path) {
            self.stats.events_for_unknown_objects += 1;
            return out;
        }
        match (e.class.as_str(), e.member.as_str()) {
            ("Object", "ChildrenChanged") => self.children_changed(e, &mut out),
            ("Object", "StateChanged") => {
                if let Some(bit) = state_from_name(&e.minor) {
                    if bit == STATE_DEFUNCT && e.detail1 != 0 {
                        self.stats.defunct_applied += 1;
                        self.drop_subtree(&e.path);
                    } else if let Some(n) = self.tree.nodes.get_mut(&e.path) {
                        let mut s = n.states.unwrap_or_default();
                        set_state(&mut s, bit, e.detail1 != 0);
                        n.states = Some(s);
                        self.stats.states_applied += 1;
                    }
                }
            }
            ("Object", "PropertyChange") => {
                let n = self.tree.nodes.get_mut(&e.path).expect("checked above");
                match (e.minor.as_str(), &e.any) {
                    ("accessible-name", AnyData::Str(s)) => {
                        n.name = Some(s.clone());
                        self.stats.names_applied += 1;
                    }
                    ("accessible-description", AnyData::Str(s)) => {
                        n.description = Some(s.clone());
                        self.stats.descriptions_applied += 1;
                    }
                    ("accessible-role", AnyData::Int(r)) => {
                        n.role = u32::try_from(*r).ok();
                        self.stats.roles_applied += 1;
                    }
                    _ => {}
                }
            }
            ("Object", "TextChanged") => self.text_changed(e, &mut out),
            _ => {}
        }
        out
    }

    fn children_changed(&mut self, e: &Event, out: &mut Vec<Repair>) {
        let parent = e.path.clone();
        let child = match &e.any {
            AnyData::Ref(r) if r.bus == self.tree.bus && !r.is_null() => r.path.clone(),
            _ => {
                self.request(out, Repair::RefetchChildren(parent));
                return;
            }
        };
        match e.minor_kind() {
            "add" => {
                if let Some(n) = self.tree.nodes.get(&child) {
                    self.stats.children_add_already_present += 1;
                    if n.parent.as_deref() != Some(parent.as_str()) {
                        self.request(out, Repair::RefetchChildren(parent));
                    }
                    return;
                }
                let len = self.tree.nodes[&parent].children.len();
                let Ok(at) = usize::try_from(e.detail1) else {
                    self.stats.children_add_index_did_not_fit += 1;
                    self.request(out, Repair::RefetchChildren(parent));
                    return;
                };
                if at > len {
                    self.stats.children_add_index_did_not_fit += 1;
                    self.request(out, Repair::RefetchChildren(parent));
                    return;
                }
                self.stats.children_added_at_index += 1;
                self.admit(&child, &parent, out);
                let p = self.tree.nodes.get_mut(&parent).expect("checked");
                p.children.insert(at, child);
                p.child_count = p.children.len() as i32;
                self.tree.renumber(&parent);
            }
            "remove" => {
                if self.tree.nodes.contains_key(&child) {
                    self.stats.children_removed += 1;
                    self.drop_subtree(&child);
                    if let Some(p) = self.tree.nodes.get_mut(&parent) {
                        p.child_count = p.children.len() as i32;
                    }
                } else {
                    self.stats.children_remove_for_unknown_child += 1;
                }
                self.pool.remove(&child);
            }
            _ => {}
        }
    }

    fn text_changed(&mut self, e: &Event, out: &mut Vec<Repair>) {
        let n = self.tree.nodes.get_mut(&e.path).expect("checked above");
        let Some(text) = n.text.as_mut() else {
            return;
        };
        let AnyData::Str(changed) = &e.any else {
            self.stats.text_patch_refused += 1;
            self.request(out, Repair::RefetchText(e.path.clone()));
            return;
        };
        if patch_text(text, e.minor_kind(), e.detail1, e.detail2, changed) {
            self.stats.text_patched += 1;
        } else {
            self.stats.text_patch_refused += 1;
            self.request(out, Repair::RefetchText(e.path.clone()));
        }
    }

    /// Makes `parent`'s children the live list: drops the subtrees of
    /// children no longer listed, admits new ones, and moves children listed
    /// here that the mirror had under another parent.
    pub fn reconcile_children(&mut self, parent: &str, live: &[ObjRef]) -> Vec<Repair> {
        let mut out = Vec::new();
        if !self.tree.nodes.contains_key(parent) {
            return out;
        }
        let live: Vec<String> = live
            .iter()
            .filter(|r| r.bus == self.tree.bus && !r.is_null())
            .map(|r| r.path.clone())
            .collect();
        let old = self.tree.nodes[parent].children.clone();
        let mut new = Vec::new();
        // Moves first, so that a child moved out of a subtree about to be
        // dropped survives the drop.
        for c in &live {
            match self.tree.nodes.get(c).map(|n| n.parent.clone()) {
                None => new.push(c.clone()),
                Some(p) if p.as_deref() != Some(parent) => {
                    self.stats.reconcile_moved += 1;
                    if let Some(old_parent) = p
                        && let Some(op) = self.tree.nodes.get_mut(&old_parent)
                    {
                        op.children.retain(|x| x != c);
                        op.child_count = op.children.len() as i32;
                        self.tree.renumber(&old_parent);
                    }
                    if let Some(n) = self.tree.nodes.get_mut(c) {
                        n.parent = Some(parent.to_owned());
                    }
                }
                Some(_) => {}
            }
        }
        for c in old.iter().filter(|c| !live.contains(c)) {
            self.stats.reconcile_removed += 1;
            self.drop_subtree(c);
        }
        for c in &new {
            self.stats.reconcile_added += 1;
            self.admit(c, parent, &mut out);
        }
        let p = self.tree.nodes.get_mut(parent).expect("checked");
        p.children = live;
        p.child_count = p.children.len() as i32;
        self.tree.renumber(parent);
        out
    }

    /// Takes in a walked subtree whose root is already in the mirror. The
    /// root keeps its place; everything under it is replaced.
    pub fn admit_subtree(&mut self, sub: Tree) {
        let root = sub.root.clone();
        let Some(old) = self.tree.nodes.get(&root).cloned() else {
            return;
        };
        for c in &old.children {
            if !sub.nodes.contains_key(c) {
                self.tree.remove_subtree(c);
            }
        }
        for (path, mut node) in sub.nodes {
            self.pool.remove(&path);
            if path == root {
                node.parent = old.parent.clone();
                node.index_in_parent = old.index_in_parent;
            }
            self.tree.nodes.insert(path, node);
        }
    }

    /// The subtree root could not be fetched, so the object is gone.
    pub fn forget(&mut self, path: &str) {
        self.pool.remove(path);
        self.drop_subtree(path);
    }

    pub fn set_text(&mut self, path: &str, text: String) {
        if let Some(n) = self.tree.nodes.get_mut(path) {
            n.text = Some(text);
        }
    }
}

/// Applies a text insertion or deletion at offsets counted in UTF-16 code
/// units, which is what Gecko reports. Refuses an edit that does not fit the
/// text as mirrored, so the caller fetches the text again instead of
/// corrupting it.
pub fn patch_text(text: &mut String, kind: &str, offset: i32, length: i32, changed: &str) -> bool {
    let Ok(offset) = usize::try_from(offset) else {
        return false;
    };
    let Ok(length) = usize::try_from(length) else {
        return false;
    };
    let mut units: Vec<u16> = text.encode_utf16().collect();
    let changed_units: Vec<u16> = changed.encode_utf16().collect();
    match kind {
        "insert" => {
            if offset > units.len() {
                return false;
            }
            units.splice(offset..offset, changed_units);
        }
        "delete" => {
            let end = offset + length;
            if end > units.len() {
                return false;
            }
            if !changed_units.is_empty() && units[offset..end] != changed_units[..] {
                return false;
            }
            units.drain(offset..end);
        }
        _ => return false,
    }
    match String::from_utf16(&units) {
        Ok(s) => {
            *text = s;
            true
        }
        Err(_) => false,
    }
}

/// The snapshot patched from Cache signals alone, as a baseline: every
/// `AddAccessible` inserted at its index, every `RemoveAccessible` applied.
pub struct NaiveMirror {
    pub tree: Tree,
}

impl NaiveMirror {
    pub fn apply_add_accessible(&mut self, item: &CacheItem) {
        if item.object.bus != self.tree.bus {
            return;
        }
        let path = item.object.path.clone();
        let node = Node::from_item(item, &self.tree.bus);
        if let Some(parent) = node.parent.clone()
            && let Some(p) = self.tree.nodes.get_mut(&parent)
            && !p.children.contains(&path)
        {
            let at = usize::try_from(item.index_in_parent)
                .unwrap_or(usize::MAX)
                .min(p.children.len());
            p.children.insert(at, path.clone());
        }
        let children = self
            .tree
            .nodes
            .get(&path)
            .map(|n| n.children.clone())
            .unwrap_or_default();
        self.tree.nodes.insert(path, Node { children, ..node });
    }

    pub fn apply_remove_accessible(&mut self, r: &ObjRef) {
        if r.bus == self.tree.bus {
            self.tree.remove_subtree(&r.path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::consts::ROOT_PATH;

    const APP: &str = ":1.7";

    fn r(p: &str) -> ObjRef {
        ObjRef {
            bus: APP.into(),
            path: p.into(),
        }
    }

    fn node(parent: Option<&str>, children: &[&str]) -> Node {
        Node {
            parent: parent.map(str::to_owned),
            children: children.iter().map(|c| (*c).to_owned()).collect(),
            role: Some(1),
            name: Some(String::new()),
            states: Some([0, 0]),
            text: Some(String::new()),
            ..Node::default()
        }
    }

    /// root -> doc -> (a -> a1, b)
    fn mirror() -> Mirror {
        let mut t = Tree::new(APP, ROOT_PATH);
        t.nodes.insert(ROOT_PATH.into(), node(None, &["/doc"]));
        t.nodes
            .insert("/doc".into(), node(Some(ROOT_PATH), &["/a", "/b"]));
        t.nodes.insert("/a".into(), node(Some("/doc"), &["/a1"]));
        t.nodes.insert("/a1".into(), node(Some("/a"), &[]));
        t.nodes.insert("/b".into(), node(Some("/doc"), &[]));
        t.renumber_all();
        Mirror::new(t)
    }

    fn ev(member: &str, minor: &str, path: &str, d1: i32, d2: i32, any: AnyData) -> Event {
        Event {
            sender: APP.into(),
            path: path.into(),
            class: "Object".into(),
            member: member.into(),
            minor: minor.into(),
            detail1: d1,
            detail2: d2,
            any,
        }
    }

    fn item(path: &str, parent: &str, index: i32, count: i32) -> CacheItem {
        CacheItem {
            object: r(path),
            app: r(ROOT_PATH),
            parent: r(parent),
            index_in_parent: index,
            child_count: count,
            interfaces: 0,
            name: format!("name of {path}"),
            role: 73,
            description: String::new(),
            states: [0, 0],
        }
    }

    #[test]
    fn removal_drops_the_whole_subtree_and_renumbers() {
        let mut m = mirror();
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "remove:system",
            "/doc",
            0,
            0,
            AnyData::Ref(r("/a")),
        ));
        assert!(repairs.is_empty());
        assert!(!m.tree.nodes.contains_key("/a1"));
        assert_eq!(m.tree.nodes["/doc"].children, vec!["/b"]);
        assert_eq!(m.tree.nodes["/b"].index_in_parent, 0);
        assert_eq!(m.stats.objects_dropped_with_subtrees, 2);
    }

    #[test]
    fn add_accessible_alone_admits_nothing() {
        let mut m = mirror();
        m.apply_add_accessible(item("/phantom", "/b", 0, 0));
        assert!(!m.tree.nodes.contains_key("/phantom"));
        assert_eq!(m.pool_len(), 1);
    }

    #[test]
    fn children_changed_admits_a_pooled_leaf_without_calls() {
        let mut m = mirror();
        m.apply_add_accessible(item("/new", "/doc", 1, 0));
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "add:system",
            "/doc",
            1,
            0,
            AnyData::Ref(r("/new")),
        ));
        assert!(repairs.is_empty());
        assert_eq!(m.tree.nodes["/doc"].children, vec!["/a", "/new", "/b"]);
        assert_eq!(m.tree.nodes["/b"].index_in_parent, 2);
        assert_eq!(m.tree.nodes["/new"].name.as_deref(), Some("name of /new"));
    }

    #[test]
    fn an_added_container_needs_its_subtree_walked() {
        let mut m = mirror();
        m.apply_add_accessible(item("/sec", "/doc", 0, 3));
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/doc",
            0,
            0,
            AnyData::Ref(r("/sec")),
        ));
        assert_eq!(repairs, vec![Repair::FetchSubtree("/sec".into())]);
        // An unannounced object gets a walk too.
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/b",
            0,
            0,
            AnyData::Ref(r("/x")),
        ));
        assert_eq!(repairs, vec![Repair::FetchSubtree("/x".into())]);
    }

    #[test]
    fn an_index_that_does_not_fit_refetches_the_parent() {
        let mut m = mirror();
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/doc",
            -1,
            0,
            AnyData::Ref(r("/leaf")),
        ));
        assert_eq!(repairs, vec![Repair::RefetchChildren("/doc".into())]);
        assert!(!m.tree.nodes.contains_key("/leaf"));
    }

    #[test]
    fn reconcile_follows_the_live_list() {
        let mut m = mirror();
        m.apply_add_accessible(item("/c", "/doc", 0, 0));
        let repairs = m.reconcile_children("/doc", &[r("/c"), r("/b"), r("/a1")]);
        assert!(repairs.is_empty());
        assert_eq!(m.tree.nodes["/doc"].children, vec!["/c", "/b", "/a1"]);
        assert!(!m.tree.nodes.contains_key("/a"));
        assert_eq!(m.stats.reconcile_moved, 1);
        assert_eq!(m.stats.objects_dropped_with_subtrees, 1);
        assert_eq!(m.tree.nodes["/a1"].parent.as_deref(), Some("/doc"));
        assert_eq!(m.tree.nodes["/a1"].index_in_parent, 2);
    }

    #[test]
    fn walked_subtrees_replace_placeholders() {
        let mut m = mirror();
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/b",
            0,
            0,
            AnyData::Ref(r("/x")),
        ));
        assert_eq!(repairs.len(), 1);
        let mut sub = Tree::new(APP, "/x");
        sub.nodes.insert("/x".into(), node(None, &["/x1"]));
        sub.nodes.insert("/x1".into(), node(Some("/x"), &[]));
        m.admit_subtree(sub);
        assert_eq!(m.tree.nodes["/x"].parent.as_deref(), Some("/b"));
        assert_eq!(m.tree.nodes["/x"].children, vec!["/x1"]);
        assert_eq!(m.tree.reachable().len(), 7);
    }

    #[test]
    fn state_name_and_defunct_events() {
        let mut m = mirror();
        m.apply_event(&ev("StateChanged", "focused", "/b", 1, 0, AnyData::Int(0)));
        assert_eq!(m.tree.nodes["/b"].states, Some([1 << 12, 0]));
        m.apply_event(&ev(
            "PropertyChange",
            "accessible-name",
            "/b",
            0,
            0,
            AnyData::Str("Renamed".into()),
        ));
        assert_eq!(m.tree.nodes["/b"].name.as_deref(), Some("Renamed"));
        m.apply_event(&ev("StateChanged", "defunct", "/a", 1, 0, AnyData::Int(0)));
        assert!(!m.tree.nodes.contains_key("/a1"));
    }

    #[test]
    fn text_is_patched_in_utf16_units() {
        let mut t = String::from("a\u{1F600}b");
        assert!(patch_text(&mut t, "insert", 3, 2, "XY"));
        assert_eq!(t, "a\u{1F600}XYb");
        assert!(patch_text(&mut t, "delete", 1, 2, "\u{1F600}"));
        assert_eq!(t, "aXYb");
        // A deletion that does not match the mirrored text is refused.
        assert!(!patch_text(&mut t, "delete", 0, 2, "zz"));
        assert!(!patch_text(&mut t, "insert", 9, 1, "q"));
        assert_eq!(t, "aXYb");
    }

    #[test]
    fn text_events_patch_or_ask_for_a_refetch() {
        let mut m = mirror();
        m.set_text("/b", "Hello".into());
        let r1 = m.apply_event(&ev(
            "TextChanged",
            "insert",
            "/b",
            5,
            6,
            AnyData::Str(" world".into()),
        ));
        assert!(r1.is_empty());
        assert_eq!(m.tree.nodes["/b"].text.as_deref(), Some("Hello world"));
        let r2 = m.apply_event(&ev(
            "TextChanged",
            "delete",
            "/b",
            40,
            1,
            AnyData::Str("x".into()),
        ));
        assert_eq!(r2, vec![Repair::RefetchText("/b".into())]);
    }

    #[test]
    fn naive_mirror_never_removes_without_remove_accessible() {
        let base = mirror().tree;
        let mut n = NaiveMirror { tree: base };
        n.apply_add_accessible(&item("/new", "/doc", 0, 0));
        assert_eq!(n.tree.nodes["/doc"].children, vec!["/new", "/a", "/b"]);
        n.apply_remove_accessible(&r("/a"));
        assert!(!n.tree.nodes.contains_key("/a1"));
    }
}
