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
//! - An event from an object the mirror does not hold means the object was
//!   added without a `ChildrenChanged` the mirror saw, as a frame's document
//!   is when it finishes loading, so the object is located through its
//!   parent.
//! - A document that finishes loading is walked again, since a frame's
//!   document gains its content without a `ChildrenChanged`.
//! - Attributes change without a signal of their own: a state or property
//!   change can change them, and so can a structural change, since the
//!   bridge derives `posinset`, `setsize`, `child-item-count` and
//!   `table-cell-index` from the structure. Those are fetched again for the
//!   objects that carry them.
//! - An object listed under a new parent moves there, and leaves its old
//!   parent's list. An object listed under itself or under one of its own
//!   descendants is not moved, since that would make a cycle: the mirror is
//!   stale above it, or the application is wrong, and the parent above is
//!   fetched again instead.
//! - A call that failed is never taken as an answer. What the mirror held
//!   stays, and the repair is tried again in a later batch.
//!
//! Applying an event never makes a call. Whatever needs the application is
//! returned as a [`Repair`] for the caller to carry out and feed back.
//!
//! The mirror lives as long as the screen reader, against applications that
//! may be buggy or hostile, so everything an application can make it hold
//! is bounded by [`Limits`], and goes when its object goes.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde::Serialize;

use crate::client::FailureKind;
use crate::consts::{ROLE_TABLE, ROLE_TREE_TABLE, STATE_DEFUNCT, set_state, state_from_name};
use crate::decode::{AnyData, CacheItem, Event, ObjRef};
use crate::fetch::{Field, Known, WalkFields, WalkResult};
use crate::tree::{Node, Tree};

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Repair {
    /// Fetch this parent's live child list and reconcile against it.
    RefetchChildren(String),
    /// Walk this newly admitted object's subtree.
    FetchSubtree(String),
    /// Fetch this object's text again.
    RefetchText(String),
    /// Fetch this object's attributes again.
    RefetchAttributes(String),
    /// Find where an object the mirror does not hold belongs, through its
    /// parent, and fetch that parent's children again.
    Locate(String),
}

impl Repair {
    pub fn kind(&self) -> &'static str {
        match self {
            Repair::RefetchChildren(_) => "refetch_children",
            Repair::FetchSubtree(_) => "fetch_subtree",
            Repair::RefetchText(_) => "refetch_text",
            Repair::RefetchAttributes(_) => "refetch_attributes",
            Repair::Locate(_) => "locate",
        }
    }

    pub fn path(&self) -> &str {
        match self {
            Repair::RefetchChildren(p)
            | Repair::FetchSubtree(p)
            | Repair::RefetchText(p)
            | Repair::RefetchAttributes(p)
            | Repair::Locate(p) => p,
        }
    }
}

/// Bounds on what one application can make the mirror hold.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// Objects the mirror holds. Past this nothing more is admitted, and
    /// [`MaintenanceStats::objects_refused_mirror_full`] counts what was not.
    pub max_objects: usize,
    /// Cache items kept for objects not admitted yet.
    pub max_pooled: usize,
    /// Batches a cache item is kept for before it is dropped unused.
    pub pool_batches: u64,
    /// Kinds of signal counted by name; the rest are counted together.
    pub max_signal_kinds: usize,
    /// Repairs handed back for another try at each batch.
    pub max_retries_per_batch: usize,
    /// How often one repair is tried again before the mirror gives up on it.
    /// The next event from its object, which shows the object is there and
    /// changing, has it tried afresh.
    pub max_tries: u32,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_objects: 500_000,
            max_pooled: 50_000,
            pool_batches: 4,
            max_signal_kinds: 256,
            max_retries_per_batch: 64,
            max_tries: 3,
        }
    }
}

/// The key signals past [`Limits::max_signal_kinds`] are counted under.
pub const OTHER_SIGNALS: &str = "other";

#[derive(Debug, Clone, Default, Serialize)]
pub struct MaintenanceStats {
    /// Every signal from the application, by `Interface.Member:minor-kind`.
    pub signals: BTreeMap<String, usize>,
    pub events_for_unknown_objects: usize,
    pub add_accessible_pooled: usize,
    pub add_accessible_updated_existing: usize,
    pub add_accessible_pool_full: usize,
    /// Items dropped from the pool unused, or once a walk had used them.
    pub pooled_items_dropped: usize,
    /// Items dropped because an event changed their object afterwards.
    pub pooled_items_superseded: usize,
    pub remove_hints_applied: usize,
    pub remove_hints_for_unknown_objects: usize,
    pub defunct_applied: usize,
    pub children_added_at_index: usize,
    pub children_added_with_pooled_data: usize,
    pub children_add_index_did_not_fit: usize,
    pub children_add_already_present: usize,
    pub children_removed: usize,
    /// A removal from a parent the mirror had already seen the child leave.
    pub children_remove_after_move: usize,
    pub objects_dropped_with_subtrees: usize,
    pub children_remove_for_unknown_child: usize,
    pub positional_attributes_refetched: usize,
    pub documents_walked_on_load: usize,
    pub reconcile_added: usize,
    pub reconcile_removed: usize,
    pub reconcile_moved: usize,
    /// Objects a walk found under a new parent.
    pub subtree_moved: usize,
    /// Objects listed under themselves or their own descendants.
    pub cycles_refused: usize,
    pub objects_refused_mirror_full: usize,
    /// Fields a failed call left as the mirror had them.
    pub fields_kept_after_failed_call: usize,
    pub retries_scheduled: usize,
    pub retries_given_up: usize,
    /// Repairs given up on and asked for again by an event from the object.
    pub retries_revived: usize,
    pub states_applied: usize,
    pub names_applied: usize,
    pub descriptions_applied: usize,
    pub roles_applied: usize,
    pub text_patched: usize,
    pub text_patch_refused: usize,
    pub text_refetched_not_patched: usize,
    pub repairs_requested: BTreeMap<String, usize>,
}

#[derive(Debug, Clone)]
struct Pooled {
    item: CacheItem,
    batch: u64,
}

/// Cache items for objects the mirror does not hold yet, and for objects
/// admitted in the current batch whose walk has not run. An item goes once
/// any event has come from its object, held or not: a walk that took the
/// object's data from it would undo what that event changed.
#[derive(Debug, Default)]
pub struct Pool(HashMap<String, Pooled>);

impl Pool {
    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn contains(&self, path: &str) -> bool {
        self.0.contains_key(path)
    }
}

impl Known for Pool {
    fn item(&self, path: &str) -> Option<&CacheItem> {
        self.0.get(path).map(|p| &p.item)
    }
}

pub struct Mirror {
    pub tree: Tree,
    pool: Pool,
    pub stats: MaintenanceStats,
    /// Objects whose text a repair fetched since the batch before last, and
    /// since the last one. Signals arrive over the bus and replies over the
    /// peer-to-peer connection, so nothing orders a fetched text against the
    /// TextChanged signals sent before it was read; such a signal can arrive
    /// after the text that already holds its change.
    text_fetched_before: HashSet<String>,
    text_fetched: HashSet<String>,
    limits: Limits,
    batch: u64,
    /// Repairs whose calls got no answer, to try again in a later batch.
    retry: BTreeSet<Repair>,
    /// For each object, how often each kind of repair of it has failed since
    /// it last worked or the object last sent an event.
    tries: HashMap<String, BTreeMap<&'static str, u32>>,
    /// Repairs tried too often, for each object, until it sends an event.
    given_up: HashMap<String, BTreeSet<Repair>>,
}

impl Mirror {
    /// The texts of `tree` count as just fetched, since the signals that
    /// follow its build can predate them.
    pub fn new(tree: Tree) -> Self {
        Self::with_limits(tree, Limits::default())
    }

    pub fn with_limits(tree: Tree, limits: Limits) -> Self {
        let text_fetched = tree
            .nodes
            .iter()
            .filter(|(_, n)| n.text.is_some())
            .map(|(p, _)| p.clone())
            .collect();
        Self {
            tree,
            pool: Pool::default(),
            stats: MaintenanceStats::default(),
            text_fetched_before: HashSet::new(),
            text_fetched,
            limits,
            batch: 0,
            retry: BTreeSet::new(),
            tries: HashMap::new(),
            given_up: HashMap::new(),
        }
    }

    /// Marks the start of a batch of signals, after the previous batch's
    /// repairs.
    pub fn start_batch(&mut self) {
        self.batch += 1;
        self.text_fetched_before = std::mem::take(&mut self.text_fetched);
        let (now, keep) = (self.batch, self.limits.pool_batches);
        let tree = &self.tree;
        let before = self.pool.len();
        self.pool
            .0
            .retain(|p, e| !tree.nodes.contains_key(p) && e.batch + keep >= now);
        self.stats.pooled_items_dropped += before - self.pool.len();
    }

    pub fn pool(&self) -> &Pool {
        &self.pool
    }

    pub fn pool_len(&self) -> usize {
        self.pool.len()
    }

    pub fn limits(&self) -> Limits {
        self.limits
    }

    /// Whether the mirror holds as many objects as it may.
    pub fn is_full(&self) -> bool {
        self.tree.len() >= self.limits.max_objects
    }

    /// Puts a repair whose call got no answer down to be tried again at a
    /// later batch, unless it has been tried too often.
    pub fn retry_later(&mut self, r: Repair) {
        if matches!(r, Repair::Locate(_)) || !self.tree.nodes.contains_key(r.path()) {
            return;
        }
        let path = r.path().to_owned();
        let n = self
            .tries
            .entry(path.clone())
            .or_default()
            .entry(r.kind())
            .or_default();
        *n += 1;
        if *n > self.limits.max_tries {
            self.stats.retries_given_up += 1;
            self.given_up.entry(path).or_default().insert(r);
            return;
        }
        self.stats.retries_scheduled += 1;
        self.retry.insert(r);
    }

    /// The repairs due for another try, as many as one batch takes.
    pub fn due_retries(&mut self) -> Vec<Repair> {
        let mut out = Vec::new();
        while out.len() < self.limits.max_retries_per_batch
            && let Some(r) = self.retry.pop_first()
        {
            if self.tree.nodes.contains_key(r.path()) {
                out.push(r);
            }
        }
        out
    }

    pub fn retries_pending(&self) -> usize {
        self.retry.len()
    }

    fn succeeded(&mut self, r: &Repair) {
        let path = r.path();
        if let Some(t) = self.tries.get_mut(path) {
            t.remove(r.kind());
            if t.is_empty() {
                self.tries.remove(path);
            }
        }
        if let Some(g) = self.given_up.get_mut(path) {
            g.remove(r);
            if g.is_empty() {
                self.given_up.remove(path);
            }
        }
        self.retry.remove(r);
    }

    /// After an event from `path`: its repairs count their tries afresh, and
    /// those given up on are asked for again.
    fn revive(&mut self, path: &str, out: &mut Vec<Repair>) {
        self.tries.remove(path);
        for r in self.given_up.remove(path).unwrap_or_default() {
            self.stats.retries_revived += 1;
            self.request(out, r);
        }
    }

    fn count_signal(&mut self, key: String) {
        let signals = &mut self.stats.signals;
        if let Some(n) = signals.get_mut(&key) {
            *n += 1;
            return;
        }
        let key = if signals.len() < self.limits.max_signal_kinds {
            key
        } else {
            OTHER_SIGNALS.to_owned()
        };
        *signals.entry(key).or_default() += 1;
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
        let path = item.object.path.clone();
        if let Some(n) = self.tree.nodes.get_mut(&path) {
            n.role = Some(item.role);
            n.name = Some(item.name.clone());
            n.description = Some(item.description.clone());
            n.states = Some(item.states);
            n.interfaces = Some(item.interfaces);
            self.stats.add_accessible_updated_existing += 1;
            self.pool.0.remove(&path);
            return;
        }
        if self.pool.len() >= self.limits.max_pooled && !self.pool.contains(&path) {
            self.stats.add_accessible_pool_full += 1;
            return;
        }
        self.stats.add_accessible_pooled += 1;
        let batch = self.batch;
        self.pool.0.insert(path, Pooled { item, batch });
    }

    pub fn apply_remove_accessible(&mut self, r: &ObjRef) {
        self.count_signal("Cache.RemoveAccessible".into());
        self.pool.0.remove(&r.path);
        if r.bus != self.tree.bus || !self.tree.nodes.contains_key(&r.path) {
            self.stats.remove_hints_for_unknown_objects += 1;
            return;
        }
        self.stats.remove_hints_applied += 1;
        self.drop_subtree(&r.path);
    }

    fn drop_subtree(&mut self, path: &str) {
        let removed = self.tree.remove_subtree(path);
        self.forget_removed(removed);
    }

    /// Lets go of everything kept for objects that have gone.
    fn forget_removed(&mut self, removed: Vec<String>) {
        self.stats.objects_dropped_with_subtrees += removed.len();
        for p in &removed {
            self.pool.0.remove(p);
            self.text_fetched.remove(p);
            self.text_fetched_before.remove(p);
            self.tries.remove(p);
            self.given_up.remove(p);
        }
        if !self.retry.is_empty() {
            let gone: HashSet<&str> = removed.iter().map(String::as_str).collect();
            self.retry.retain(|r| !gone.contains(r.path()));
        }
    }

    /// A node for a newly admitted object, from its pooled item where there
    /// is one, and the walk that completes it. A cache item carries neither
    /// attributes nor text, so even a pooled leaf needs that walk; with the
    /// item it asks only for what the item lacks. Returns false when the
    /// mirror is full.
    fn admit(&mut self, path: &str, parent: &str, out: &mut Vec<Repair>) -> bool {
        if self.is_full() {
            self.stats.objects_refused_mirror_full += 1;
            return false;
        }
        let bus = self.tree.bus.clone();
        let node = match self.pool.item(path) {
            Some(item) => {
                self.stats.children_added_with_pooled_data += 1;
                Node::from_item(item, &bus)
            }
            None => Node::default(),
        };
        self.tree.nodes.insert(
            path.to_owned(),
            Node {
                parent: Some(parent.to_owned()),
                children_unknown: true,
                ..node
            },
        );
        self.request(out, Repair::FetchSubtree(path.to_owned()));
        true
    }

    pub fn apply_event(&mut self, e: &Event) -> Vec<Repair> {
        let mut out = Vec::new();
        if e.sender != self.tree.bus {
            return out;
        }
        self.count_signal(format!("{}.{}:{}", e.class, e.member, e.minor_kind()));
        // The object changed after any item made for it, so a walk that took
        // its data from the item would undo the change, whether the object is
        // still to be admitted or was admitted in this batch. A new parent
        // changes nothing the item gives the mirror, and comes with every
        // insertion.
        let changes = !(e.member == "PropertyChange" && e.minor == "accessible-parent");
        if changes && self.pool.0.remove(&e.path).is_some() {
            self.stats.pooled_items_superseded += 1;
        }
        if !self.tree.nodes.contains_key(&e.path) {
            self.stats.events_for_unknown_objects += 1;
            self.request(&mut out, Repair::Locate(e.path.clone()));
            return out;
        }
        if changes {
            self.revive(&e.path, &mut out);
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
                        // aria-pressed, for one, is a state and an attribute.
                        self.request(&mut out, Repair::RefetchAttributes(e.path.clone()));
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
                // A name from aria-label, for one, adds explicit-name. The
                // flood of accessible-parent changes that follows activation
                // changes nothing the mirror holds.
                if matches!(
                    e.minor.as_str(),
                    "accessible-name" | "accessible-description" | "accessible-role"
                ) {
                    self.request(&mut out, Repair::RefetchAttributes(e.path.clone()));
                }
            }
            ("Object", "TextChanged") => self.text_changed(e, &mut out),
            // A frame's document fills in when it finishes loading, with no
            // ChildrenChanged for what it gained, so it is walked again.
            ("Document", "LoadComplete" | "Reload") => {
                self.stats.documents_walked_on_load += 1;
                self.request(&mut out, Repair::FetchSubtree(e.path.clone()));
            }
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
                if !self.admit(&child, &parent, out) {
                    if let Some(p) = self.tree.nodes.get_mut(&parent) {
                        p.children_unknown = true;
                    }
                    return;
                }
                let p = self.tree.nodes.get_mut(&parent).expect("checked");
                p.children.insert(at, child);
                counted(p, 1);
                self.tree.renumber(&parent);
                self.refresh_positions(&parent, out);
            }
            "remove" => {
                match self.tree.nodes.get(&child).map(|n| n.parent.clone()) {
                    Some(Some(p)) if p == parent => {
                        self.stats.children_removed += 1;
                        self.drop_subtree(&child);
                        self.unlisted(&parent, -1, out);
                    }
                    // The mirror already has the child where it went, from
                    // a walk or a child list fetched since it moved; only a
                    // stale listing here can be left to clean up.
                    Some(_) => {
                        self.stats.children_remove_after_move += 1;
                        if let Some(p) = self.tree.nodes.get_mut(&parent)
                            && p.children.contains(&child)
                        {
                            p.children.retain(|c| *c != child);
                            self.unlisted(&parent, 0, out);
                        }
                    }
                    None => self.stats.children_remove_for_unknown_child += 1,
                }
                self.pool.0.remove(&child);
            }
            _ => {}
        }
    }

    /// After a child left `parent`'s list: its count, its children's
    /// places, and the attributes derived from them. `by` is how the
    /// application's own count changed.
    fn unlisted(&mut self, parent: &str, by: i32, out: &mut Vec<Repair>) {
        if let Some(p) = self.tree.nodes.get_mut(parent) {
            counted(p, by);
            self.tree.renumber(parent);
            self.refresh_positions(parent, out);
        }
    }

    fn text_changed(&mut self, e: &Event, out: &mut Vec<Repair>) {
        // A text fetched since the last batch may already hold this change,
        // and patching it again would apply it twice.
        if self.text_fetched_before.contains(&e.path) {
            self.stats.text_refetched_not_patched += 1;
            self.request(out, Repair::RefetchText(e.path.clone()));
            return;
        }
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

    /// An object the application lists under `parent` that would close a
    /// cycle: `parent` itself or one of its ancestors. Either the mirror is
    /// stale above `parent`, as when the application moved `parent` out from
    /// under that object and the mirror has not seen it yet, or the
    /// application is wrong. The object stays where it is; the list it is in
    /// now is fetched again, and `parent`'s in a later batch, once the part
    /// above has been put right.
    fn refuse_cycle(&mut self, parent: &str, listed: &str, out: &mut Vec<Repair>) {
        self.stats.cycles_refused += 1;
        if listed == parent {
            return;
        }
        if let Some(above) = self.tree.nodes.get(listed).and_then(|n| n.parent.clone()) {
            self.request(out, Repair::RefetchChildren(above));
        }
        self.retry_later(Repair::RefetchChildren(parent.to_owned()));
    }

    /// Makes `parent`'s children the live list: drops the subtrees of
    /// children no longer listed, admits new ones, and moves children listed
    /// here that the mirror had under another parent.
    pub fn reconcile_children(&mut self, parent: &str, live: &[ObjRef]) -> Vec<Repair> {
        let mut out = Vec::new();
        if !self.tree.nodes.contains_key(parent) {
            return out;
        }
        let mut listed = HashSet::new();
        let live: Vec<String> = live
            .iter()
            .filter(|r| r.bus == self.tree.bus && !r.is_null() && listed.insert(r.path.as_str()))
            .map(|r| r.path.clone())
            .collect();
        let mut above: HashSet<String> = self.tree.ancestors(parent).into_iter().collect();
        above.insert(parent.to_owned());
        let old = self.tree.nodes[parent].children.clone();
        let mut kept = Vec::with_capacity(live.len());
        let mut new = HashSet::new();
        let mut left = BTreeSet::new();
        let mut cycles = Vec::new();
        // Moves first, so that a child moved out of a subtree about to be
        // dropped survives the drop.
        for c in &live {
            if above.contains(c) {
                cycles.push(c.clone());
                continue;
            }
            match self.tree.nodes.get(c).map(|n| n.parent.clone()) {
                None => {
                    new.insert(c.clone());
                }
                Some(p) if p.as_deref() != Some(parent) => {
                    self.stats.reconcile_moved += 1;
                    if let Some(old_parent) = p
                        && let Some(op) = self.tree.nodes.get_mut(&old_parent)
                    {
                        op.children.retain(|x| x != c);
                        counted(op, -1);
                        self.tree.renumber(&old_parent);
                        left.insert(old_parent);
                    }
                    if let Some(n) = self.tree.nodes.get_mut(c) {
                        n.parent = Some(parent.to_owned());
                    }
                }
                Some(_) => {}
            }
            kept.push(c.clone());
        }
        for c in old.iter().filter(|c| !listed.contains(c.as_str())) {
            // Only an object that is really here goes; a stale listing of
            // one that lives elsewhere just leaves the list.
            if self
                .tree
                .nodes
                .get(c)
                .is_some_and(|n| n.parent.as_deref() == Some(parent))
            {
                self.stats.reconcile_removed += 1;
                self.drop_subtree(c);
            }
        }
        let mut refused = HashSet::new();
        for c in &kept {
            if new.contains(c) {
                if self.admit(c, parent, &mut out) {
                    self.stats.reconcile_added += 1;
                } else {
                    refused.insert(c.clone());
                }
            }
        }
        kept.retain(|c| !refused.contains(c));
        let changed = old != kept;
        let p = self.tree.nodes.get_mut(parent).expect("checked");
        p.children = kept;
        p.children_unknown = !refused.is_empty() || !cycles.is_empty();
        p.child_count = if p.children_unknown {
            live.len() as i32
        } else {
            p.children.len() as i32
        };
        self.tree.renumber(parent);
        if changed {
            self.refresh_positions(parent, &mut out);
        }
        for op in left {
            if op != parent && self.tree.nodes.contains_key(&op) {
                self.refresh_positions(&op, &mut out);
            }
        }
        if cycles.is_empty() {
            self.succeeded(&Repair::RefetchChildren(parent.to_owned()));
        }
        for c in cycles {
            self.refuse_cycle(parent, &c, &mut out);
        }
        out
    }

    /// After `parent`'s children changed, fetches the attributes the bridge
    /// derives from the structure again: the parent's child count, its
    /// children's places in their set, and, inside a table, every cell's
    /// index, since a row or cell added anywhere shifts all that follow.
    fn refresh_positions(&mut self, parent: &str, out: &mut Vec<Repair>) {
        let has = |n: &Node, keys: &[&str]| {
            n.attributes
                .as_ref()
                .is_some_and(|a| a.iter().any(|(k, _)| keys.contains(&k.as_str())))
        };
        let mut paths = Vec::new();
        if let Some(p) = self.tree.nodes.get(parent) {
            if has(p, &["child-item-count"]) {
                paths.push(parent.to_owned());
            }
            for c in &p.children {
                if self
                    .tree
                    .nodes
                    .get(c)
                    .is_some_and(|n| has(n, &["posinset", "setsize"]))
                {
                    paths.push(c.clone());
                }
            }
        }
        let is_table = |p: &String| {
            self.tree
                .nodes
                .get(p)
                .is_some_and(|n| matches!(n.role, Some(ROLE_TABLE | ROLE_TREE_TABLE)))
        };
        let table = std::iter::once(parent.to_owned())
            .chain(self.tree.ancestors(parent))
            .find(is_table);
        if let Some(table) = table {
            for q in self.tree.subtree(&table) {
                if self
                    .tree
                    .nodes
                    .get(&q)
                    .is_some_and(|m| has(m, &["table-cell-index", "child-item-count", "posinset"]))
                {
                    paths.push(q);
                }
            }
        }
        paths.sort();
        paths.dedup();
        for p in paths {
            self.stats.positional_attributes_refetched += 1;
            self.request(out, Repair::RefetchAttributes(p));
        }
    }

    /// Takes in a walk of a subtree whose root the mirror holds. The root
    /// keeps its place. Below it the walk is the authority wherever it got an
    /// answer: an object it no longer finds is dropped, at any depth; one it
    /// finds under a new parent moves there; a field it fetched replaces the
    /// mirror's. Wherever a call failed, what the mirror had stays as it was
    /// and the object is fetched again in a later batch, so a slow
    /// application never empties the mirror.
    pub fn admit_subtree(&mut self, walk: WalkResult) -> Vec<Repair> {
        let mut out = Vec::new();
        let WalkResult {
            tree: sub,
            failed,
            fields,
            ..
        } = walk;
        let root = sub.root.clone();
        let Some(old_root) = self.tree.nodes.get(&root).cloned() else {
            return out;
        };
        let above: HashSet<String> = self.tree.ancestors(&root).into_iter().collect();
        let listed_by_walk = |p: &str| {
            sub.nodes.contains_key(p)
                && !failed
                    .get(p)
                    .is_some_and(|f| f.contains_key(&Field::Children))
        };
        // Where the walk puts each object it reached, leaving out what would
        // close a cycle and everything the walk found below that.
        let mut walked: HashMap<String, String> = HashMap::new();
        let mut refused: Vec<(String, String)> = Vec::new();
        let mut stack = vec![root.clone()];
        let mut seen = HashSet::new();
        while let Some(p) = stack.pop() {
            if !seen.insert(p.clone()) || !listed_by_walk(&p) {
                continue;
            }
            for c in &sub.nodes[&p].children {
                if above.contains(c) || *c == root {
                    refused.push((p.clone(), c.clone()));
                } else if !walked.contains_key(c) {
                    walked.insert(c.clone(), p.clone());
                    stack.push(c.clone());
                }
            }
        }
        // The new subtree: the walk's child lists where it got them, and
        // the mirror's where it did not, less what the walk put elsewhere.
        let mut lists: Vec<(String, Vec<String>)> = Vec::new();
        let mut parent_of: HashMap<String, String> = HashMap::new();
        let mut reached = HashSet::from([root.clone()]);
        let mut stack = vec![root.clone()];
        while let Some(p) = stack.pop() {
            let list: Vec<String> = if listed_by_walk(&p) {
                sub.nodes[&p]
                    .children
                    .iter()
                    .filter(|c| walked.get(*c) == Some(&p))
                    .cloned()
                    .collect()
            } else {
                self.tree
                    .nodes
                    .get(&p)
                    .map(|n| {
                        n.children
                            .iter()
                            .filter(|c| {
                                walked.get(*c).is_none_or(|w| *w == p)
                                    && self
                                        .tree
                                        .nodes
                                        .get(*c)
                                        .is_some_and(|k| k.parent.as_ref() == Some(&p))
                            })
                            .cloned()
                            .collect()
                    })
                    .unwrap_or_default()
            };
            let list: Vec<String> = list
                .into_iter()
                .filter(|c| reached.insert(c.clone()))
                .collect();
            for c in &list {
                parent_of.insert(c.clone(), p.clone());
                stack.push(c.clone());
            }
            lists.push((p, list));
        }
        // Objects the mirror holds elsewhere that the walk found here.
        let moved: Vec<String> = parent_of
            .iter()
            .filter(|(c, p)| {
                self.tree
                    .nodes
                    .get(*c)
                    .is_some_and(|n| n.parent.as_ref() != Some(*p))
            })
            .map(|(c, _)| c.clone())
            .collect();
        let mut before = self.tree.subtree(&root);
        for c in &moved {
            before.extend(self.tree.subtree(c));
        }
        let gone: Vec<String> = before
            .into_iter()
            .filter(|p| !reached.contains(p))
            .collect();
        let arriving = reached
            .iter()
            .filter(|p| !self.tree.nodes.contains_key(*p))
            .count();
        if self.tree.len() - gone.len() + arriving > self.limits.max_objects {
            self.stats.objects_refused_mirror_full += arriving;
            if let Some(n) = self.tree.nodes.get_mut(&root) {
                n.children_unknown = true;
            }
            return out;
        }
        let mut left = BTreeSet::new();
        for c in &moved {
            self.stats.subtree_moved += 1;
            let old_parent = self.tree.nodes.get(c).and_then(|n| n.parent.clone());
            if let Some(op) = old_parent
                && let Some(n) = self.tree.nodes.get_mut(&op)
            {
                n.children.retain(|x| x != c);
                counted(n, -1);
                self.tree.renumber(&op);
                left.insert(op);
            }
        }
        for p in &gone {
            self.tree.nodes.remove(p);
        }
        self.forget_removed(gone);
        for (p, list) in &lists {
            let in_walk = sub
                .nodes
                .get(p)
                .filter(|_| *p == root || walked.contains_key(p));
            // A list the walk did not get is the mirror's where it had one,
            // and unknown for an object it did not hold; then the count the
            // application gave, if any, says more than the empty list.
            let listed = listed_by_walk(p);
            let held = self.tree.nodes.get(p);
            let unknown = !listed && held.is_none_or(|o| o.children_unknown);
            let recount =
                listed || held.is_some_and(|o| !o.children_unknown && o.children != *list);
            let mut node = match (held, in_walk) {
                (Some(old), Some(w)) => {
                    let (n, kept) = merge(old, w, failed.get(p), fields);
                    self.stats.fields_kept_after_failed_call += kept;
                    n
                }
                (None, Some(w)) => w.clone(),
                (Some(old), None) => old.clone(),
                (None, None) => continue,
            };
            if recount {
                node.child_count = list.len() as i32;
            }
            node.children = list.clone();
            node.children_unknown = unknown;
            if *p == root {
                node.parent = old_root.parent.clone();
                node.index_in_parent = old_root.index_in_parent;
            } else {
                node.parent = parent_of.get(p).cloned();
            }
            if in_walk.is_some() {
                self.pool.0.remove(p);
                let text_fetched =
                    fields.text && !failed.get(p).is_some_and(|f| f.contains_key(&Field::Text));
                if text_fetched && node.text.is_some() {
                    self.text_fetched.insert(p.clone());
                }
            }
            self.tree.nodes.insert(p.clone(), node);
        }
        for (p, _) in &lists {
            self.tree.renumber(p);
        }
        for op in left {
            if !reached.contains(&op) && self.tree.nodes.contains_key(&op) {
                self.refresh_positions(&op, &mut out);
            }
        }
        for (p, c) in refused {
            if reached.contains(&p) {
                if let Some(n) = self.tree.nodes.get_mut(&p) {
                    n.children_unknown = true;
                }
                self.refuse_cycle(&p, &c, &mut out);
            }
        }
        for (p, _) in &lists {
            if *p != root && !walked.contains_key(p) {
                continue;
            }
            let Some(f) = failed.get(p) else {
                self.succeeded(&Repair::FetchSubtree(p.clone()));
                if fields.attributes {
                    self.succeeded(&Repair::RefetchAttributes(p.clone()));
                }
                if fields.text {
                    self.succeeded(&Repair::RefetchText(p.clone()));
                }
                continue;
            };
            let again: Vec<Field> = f
                .iter()
                .filter(|(_, k)| k.worth_retrying())
                .map(|(f, _)| *f)
                .collect();
            let repair = if again
                .iter()
                .any(|f| !matches!(f, Field::Attributes | Field::Text))
            {
                Some(Repair::FetchSubtree(p.clone()))
            } else if again.contains(&Field::Attributes) {
                Some(Repair::RefetchAttributes(p.clone()))
            } else if again.contains(&Field::Text) {
                Some(Repair::RefetchText(p.clone()))
            } else {
                None
            };
            if let Some(r) = repair {
                self.retry_later(r);
            }
        }
        out
    }

    /// The subtree root could not be fetched, so the object is gone.
    pub fn forget(&mut self, path: &str) {
        self.pool.0.remove(path);
        self.drop_subtree(path);
    }

    pub fn set_text(&mut self, path: &str, text: String) {
        if let Some(n) = self.tree.nodes.get_mut(path) {
            n.text = Some(text);
            self.text_fetched.insert(path.to_owned());
            self.succeeded(&Repair::RefetchText(path.to_owned()));
        }
    }

    pub fn set_attributes(&mut self, path: &str, attributes: Vec<(String, String)>) {
        if let Some(n) = self.tree.nodes.get_mut(path) {
            n.attributes = Some(attributes);
            self.succeeded(&Repair::RefetchAttributes(path.to_owned()));
        }
    }
}

/// After a child joined or left `n`'s list, by `by`. A list the mirror does
/// not know keeps the count the application gave, changed by as much.
fn counted(n: &mut Node, by: i32) {
    n.child_count = if n.children_unknown {
        (n.child_count + by).max(0)
    } else {
        n.children.len() as i32
    };
}

/// The mirror's node with what a walk got for it. A field the walk asked for
/// and got replaces the mirror's, and so does one a cache item supplied; one
/// it did not ask for, or asked for and did not get, stays as the mirror had
/// it. Returns the node and how many fields were kept that way.
fn merge(
    old: &Node,
    new: &Node,
    failed: Option<&BTreeMap<Field, FailureKind>>,
    fields: WalkFields,
) -> (Node, usize) {
    let mut kept = 0;
    let mut pick = |f: Field, o: bool, n: bool| -> bool {
        let got = n || (fields.asks_for(f) && !failed.is_some_and(|m| m.contains_key(&f)));
        if !got && failed.is_some_and(|m| m.contains_key(&f)) && o {
            kept += 1;
        }
        got
    };
    let mut node = new.clone();
    if !pick(Field::Role, old.role.is_some(), new.role.is_some()) {
        node.role = old.role;
    }
    if !pick(Field::States, old.states.is_some(), new.states.is_some()) {
        node.states = old.states;
    }
    if !pick(Field::Name, old.name.is_some(), new.name.is_some()) {
        node.name = old.name.clone();
    }
    if !pick(
        Field::Description,
        old.description.is_some(),
        new.description.is_some(),
    ) {
        node.description = old.description.clone();
    }
    if !pick(
        Field::Interfaces,
        old.interfaces.is_some(),
        new.interfaces.is_some(),
    ) {
        node.interfaces = old.interfaces;
    }
    if !pick(
        Field::Attributes,
        old.attributes.is_some(),
        new.attributes.is_some(),
    ) {
        node.attributes = old.attributes.clone();
    }
    if !pick(Field::Text, old.text.is_some(), new.text.is_some()) {
        node.text = old.text.clone();
    }
    if failed.is_some_and(|m| m.contains_key(&Field::Children)) {
        node.child_count = old.child_count;
    }
    (node, kept)
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
    use crate::testing::returns;

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
    fn children_changed_admits_a_pooled_leaf_at_its_index() {
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
        // Only for attributes and text, which a cache item lacks.
        assert_eq!(repairs, vec![Repair::FetchSubtree("/new".into())]);
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
        assert_eq!(repairs, vec![Repair::FetchSubtree("/c".into())]);
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
        m.admit_subtree(WalkResult::from_tree(sub, WalkFields::ALL));
        assert_consistent(&m);
        assert_eq!(m.tree.nodes["/x"].parent.as_deref(), Some("/b"));
        assert_eq!(m.tree.nodes["/x"].children, vec!["/x1"]);
        assert_eq!(m.tree.reachable().len(), 7);
    }

    #[test]
    fn a_document_that_finishes_loading_is_walked_again() {
        let mut m = mirror();
        let mut e = ev(
            "LoadComplete",
            "",
            "/doc",
            0,
            0,
            AnyData::Str("Frame".into()),
        );
        e.class = "Document".into();
        assert_eq!(m.apply_event(&e), vec![Repair::FetchSubtree("/doc".into())]);
    }

    #[test]
    fn an_event_from_an_unknown_object_locates_it() {
        let mut m = mirror();
        let repairs = m.apply_event(&ev(
            "StateChanged",
            "busy",
            "/frame-doc",
            0,
            0,
            AnyData::Int(0),
        ));
        assert_eq!(repairs, vec![Repair::Locate("/frame-doc".into())]);
    }

    #[test]
    fn a_structural_change_refetches_positional_attributes() {
        let mut m = mirror();
        let attrs = |k: &str| Some(vec![(k.to_owned(), "1".to_owned())]);
        m.tree.nodes.get_mut("/doc").unwrap().attributes = attrs("child-item-count");
        m.tree.nodes.get_mut("/b").unwrap().attributes = attrs("posinset");
        m.tree.nodes.get_mut("/a1").unwrap().attributes = attrs("posinset");
        let mut repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/doc",
            0,
            0,
            AnyData::Ref(r("/a")),
        ));
        repairs.sort();
        assert_eq!(
            repairs,
            vec![
                Repair::RefetchAttributes("/b".into()),
                Repair::RefetchAttributes("/doc".into()),
            ]
        );
        // Inside a table, every cell after a change may have a new index.
        m.tree.nodes.get_mut("/doc").unwrap().role = Some(crate::consts::ROLE_TABLE);
        m.tree.nodes.get_mut("/b").unwrap().children = vec!["/cell".into()];
        let mut cell = node(Some("/b"), &[]);
        cell.attributes = attrs("table-cell-index");
        m.tree.nodes.insert("/cell".into(), cell);
        let repairs = m.reconcile_children("/doc", &[r("/row"), r("/b")]);
        assert!(repairs.contains(&Repair::FetchSubtree("/row".into())));
        assert!(repairs.contains(&Repair::RefetchAttributes("/cell".into())));
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
    fn text_fetched_since_the_last_batch_is_fetched_again_not_patched() {
        let mut m = mirror();
        m.set_text("/b", "Hello world".into());
        m.start_batch();
        let insert = ev(
            "TextChanged",
            "insert",
            "/b",
            5,
            6,
            AnyData::Str(" world".into()),
        );
        assert_eq!(
            m.apply_event(&insert),
            vec![Repair::RefetchText("/b".into())]
        );
        assert_eq!(m.tree.nodes["/b"].text.as_deref(), Some("Hello world"));
        // Two batches on, with no fetch in between, patching resumes.
        m.start_batch();
        assert!(m.apply_event(&insert).is_empty());
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

    /// Every child list names held objects whose parent is the list's owner,
    /// each once and in its place, every parent lists its children, and
    /// every held object can be reached from the root.
    fn assert_consistent(m: &Mirror) {
        let t = &m.tree;
        for (p, n) in &t.nodes {
            let mut once = n.children.clone();
            once.sort();
            once.dedup();
            assert_eq!(once.len(), n.children.len(), "{p} lists a child twice");
            for (i, c) in n.children.iter().enumerate() {
                let k = t
                    .nodes
                    .get(c)
                    .unwrap_or_else(|| panic!("{p} lists {c}, which is not held"));
                assert_eq!(k.parent.as_deref(), Some(p.as_str()), "{p} lists {c}");
                assert_eq!(k.index_in_parent, i as i32, "{c} in {p}");
            }
            if let Some(parent) = &n.parent {
                assert!(
                    t.nodes.get(parent).is_some_and(|q| q.children.contains(p)),
                    "{p}'s parent {parent} does not list it"
                );
            }
        }
        let reachable = t.reachable();
        let mut lost: Vec<&String> = t
            .nodes
            .keys()
            .filter(|k| !reachable.contains(k.as_str()))
            .collect();
        lost.sort();
        assert!(lost.is_empty(), "held but unreachable: {lost:?}");
    }

    fn sub(root: &str, nodes: &[(&str, Option<&str>, &[&str])]) -> Tree {
        let mut t = Tree::new(APP, root);
        for (p, parent, children) in nodes {
            t.nodes.insert((*p).into(), node(*parent, children));
        }
        t.renumber_all();
        t
    }

    fn walked(t: Tree) -> WalkResult {
        WalkResult::from_tree(t, WalkFields::ALL)
    }

    fn timeout() -> FailureKind {
        FailureKind::Timeout
    }

    fn sorted(mut v: Vec<Repair>) -> Vec<Repair> {
        v.sort();
        v
    }

    #[test]
    fn a_parent_that_lists_itself_is_not_made_its_own_child() {
        let m = returns(|| {
            let mut m = mirror();
            let repairs = m.reconcile_children("/b", &[r("/b")]);
            assert!(repairs.is_empty());
            m.apply_event(&ev(
                "ChildrenChanged",
                "add",
                "/b",
                0,
                0,
                AnyData::Ref(r("/x")),
            ));
            m
        });
        assert_eq!(m.tree.nodes["/b"].children, vec!["/x"]);
        assert_eq!(m.stats.cycles_refused, 1);
        assert_consistent(&m);
    }

    #[test]
    fn a_parent_that_lists_its_ancestor_asks_above_and_tries_again_later() {
        let mut m = returns(|| {
            let mut m = mirror();
            let repairs = m.reconcile_children("/b", &[r("/doc")]);
            assert_eq!(repairs, vec![Repair::RefetchChildren(ROOT_PATH.into())]);
            m.apply_event(&ev(
                "ChildrenChanged",
                "add",
                "/b",
                0,
                0,
                AnyData::Ref(r("/x")),
            ));
            m
        });
        assert_eq!(m.tree.nodes["/doc"].parent.as_deref(), Some(ROOT_PATH));
        assert_eq!(m.tree.nodes["/b"].children, vec!["/x"]);
        assert_consistent(&m);
        m.start_batch();
        assert_eq!(m.due_retries(), vec![Repair::RefetchChildren("/b".into())]);
        assert!(m.due_retries().is_empty());
    }

    #[test]
    fn a_walk_that_lists_an_ancestor_leaves_it_where_it_is() {
        let m = returns(|| {
            let mut m = mirror();
            // The walk of /b found /doc, and under it what /doc holds.
            let walk = sub(
                "/b",
                &[
                    ("/b", None, &["/doc"]),
                    ("/doc", Some("/b"), &["/a"]),
                    ("/a", Some("/doc"), &["/a1"]),
                    ("/a1", Some("/a"), &[]),
                ],
            );
            let repairs = m.admit_subtree(walked(walk));
            assert_eq!(repairs, vec![Repair::RefetchChildren(ROOT_PATH.into())]);
            m.apply_event(&ev(
                "ChildrenChanged",
                "add",
                "/b",
                0,
                0,
                AnyData::Ref(r("/x")),
            ));
            m
        });
        assert_eq!(m.tree.nodes[ROOT_PATH].children, vec!["/doc"]);
        assert_eq!(m.tree.nodes["/doc"].parent.as_deref(), Some(ROOT_PATH));
        assert_eq!(m.tree.nodes["/doc"].children, vec!["/a", "/b"]);
        assert_eq!(m.tree.nodes["/b"].children, vec!["/x"]);
        assert_eq!(m.tree.len(), 6);
        assert_consistent(&m);
    }

    #[test]
    fn a_reparent_seen_in_stale_order_is_put_right_through_the_parent_above() {
        // /a1 moved out of /a to /doc, and /a under /a1; the mirror hears
        // /a1's new children first.
        let mut m = returns(|| {
            let mut m = mirror();
            let repairs = m.reconcile_children("/a1", &[r("/a")]);
            assert_eq!(repairs, vec![Repair::RefetchChildren("/doc".into())]);
            m
        });
        assert_consistent(&m);
        assert_eq!(m.tree.nodes["/a"].children, vec!["/a1"]);
        let repairs = m.reconcile_children("/doc", &[r("/a1"), r("/b")]);
        assert!(repairs.is_empty(), "{repairs:?}");
        assert_consistent(&m);
        m.start_batch();
        let again = m.due_retries();
        assert_eq!(again, vec![Repair::RefetchChildren("/a1".into())]);
        let repairs = m.reconcile_children("/a1", &[r("/a")]);
        assert_eq!(repairs, vec![Repair::FetchSubtree("/a".into())]);
        assert_eq!(m.tree.nodes["/doc"].children, vec!["/a1", "/b"]);
        assert_eq!(m.tree.nodes["/a1"].children, vec!["/a"]);
        assert_consistent(&m);
    }

    #[test]
    fn a_walk_that_got_no_answers_changes_nothing_and_is_tried_again() {
        let mut m = mirror();
        m.tree.nodes.get_mut("/doc").unwrap().name = Some("Page".into());
        let mut w = walked(sub("/doc", &[("/doc", None, &[])]));
        let n = w.tree.nodes.get_mut("/doc").unwrap();
        (n.role, n.name, n.states, n.text) = (None, None, None, None);
        w.failed.insert(
            "/doc".into(),
            [
                Field::Children,
                Field::Role,
                Field::States,
                Field::Name,
                Field::Description,
                Field::Interfaces,
                Field::Attributes,
                Field::Text,
            ]
            .into_iter()
            .map(|f| (f, timeout()))
            .collect(),
        );
        let before = m.tree.clone();
        assert!(m.admit_subtree(w).is_empty());
        assert_eq!(before.nodes, m.tree.nodes);
        assert_eq!(m.tree.nodes["/doc"].name.as_deref(), Some("Page"));
        assert_eq!(m.tree.nodes["/doc"].role, Some(1));
        assert_consistent(&m);
        m.start_batch();
        assert_eq!(m.due_retries(), vec![Repair::FetchSubtree("/doc".into())]);
    }

    #[test]
    fn a_child_whose_children_timed_out_keeps_its_subtree() {
        let mut m = mirror();
        m.tree.nodes.get_mut("/a").unwrap().child_count = 1;
        let mut w = walked(sub(
            "/doc",
            &[
                ("/doc", None, &["/a", "/b"]),
                ("/a", Some("/doc"), &[]),
                ("/b", Some("/doc"), &[]),
            ],
        ));
        w.tree.nodes.get_mut("/a").unwrap().name = Some("fresh".into());
        w.failed
            .insert("/a".into(), [(Field::Children, timeout())].into());
        m.admit_subtree(w);
        assert_eq!(m.tree.nodes["/a"].children, vec!["/a1"]);
        assert_eq!(m.tree.nodes["/a"].child_count, 1);
        assert!(!m.tree.nodes["/a"].children_unknown);
        assert_eq!(m.tree.nodes["/a"].name.as_deref(), Some("fresh"));
        assert_eq!(m.tree.len(), 5);
        assert_consistent(&m);
        m.start_batch();
        assert_eq!(m.due_retries(), vec![Repair::FetchSubtree("/a".into())]);
    }

    #[test]
    fn a_failed_field_keeps_its_value_and_only_what_failed_is_fetched_again() {
        let mut m = mirror();
        m.tree.nodes.get_mut("/b").unwrap().attributes = Some(vec![("k".into(), "v".into())]);
        let mut w = walked(sub(
            "/doc",
            &[
                ("/doc", None, &["/a", "/b"]),
                ("/a", Some("/doc"), &["/a1"]),
                ("/a1", Some("/a"), &[]),
                ("/b", Some("/doc"), &[]),
            ],
        ));
        w.failed
            .insert("/b".into(), [(Field::Attributes, timeout())].into());
        let answer = FailureKind::ErrorReply {
            name: "org.freedesktop.DBus.Error.Failed".into(),
            message: None,
        };
        w.failed
            .insert("/a1".into(), [(Field::Attributes, answer)].into());
        m.admit_subtree(w);
        assert_eq!(
            m.tree.nodes["/b"].attributes,
            Some(vec![("k".into(), "v".into())])
        );
        assert_eq!(m.stats.fields_kept_after_failed_call, 1);
        m.start_batch();
        // An error reply is the application's answer, not one to ask again.
        assert_eq!(
            m.due_retries(),
            vec![Repair::RefetchAttributes("/b".into())]
        );
    }

    #[test]
    fn a_repair_is_tried_again_only_so_often() {
        let mut m = Mirror::with_limits(
            mirror().tree,
            Limits {
                max_tries: 2,
                max_retries_per_batch: 1,
                ..Limits::default()
            },
        );
        for _ in 0..3 {
            m.retry_later(Repair::RefetchText("/b".into()));
        }
        assert_eq!(m.stats.retries_given_up, 1);
        m.retry_later(Repair::RefetchText("/a".into()));
        m.retry_later(Repair::Locate("/a".into()));
        m.retry_later(Repair::RefetchText("/nowhere".into()));
        assert_eq!(m.retries_pending(), 2);
        assert_eq!(m.due_retries().len(), 1);
        assert_eq!(m.due_retries().len(), 1);
        assert!(m.due_retries().is_empty());
        // Once it works, it may fail and be tried again as often as before.
        m.set_text("/b", "x".into());
        m.retry_later(Repair::RefetchText("/b".into()));
        assert_eq!(m.retries_pending(), 1);
    }

    #[test]
    fn retries_go_with_their_objects() {
        let mut m = mirror();
        m.retry_later(Repair::RefetchText("/a1".into()));
        m.retry_later(Repair::FetchSubtree("/b".into()));
        m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/doc",
            0,
            0,
            AnyData::Ref(r("/a")),
        ));
        assert_eq!(m.retries_pending(), 1);
        assert_eq!(m.due_retries(), vec![Repair::FetchSubtree("/b".into())]);
    }

    #[test]
    fn a_walk_that_finds_an_object_under_a_new_parent_moves_it() {
        let mut m = mirror();
        let w = walked(sub(
            "/b",
            &[("/b", None, &["/a1"]), ("/a1", Some("/b"), &[])],
        ));
        m.admit_subtree(w);
        assert!(m.tree.nodes["/a"].children.is_empty());
        assert_eq!(m.tree.nodes["/a"].child_count, 0);
        assert_eq!(m.tree.nodes["/b"].children, vec!["/a1"]);
        assert_eq!(m.stats.subtree_moved, 1);
        assert_consistent(&m);
        // The old parent going no longer takes the object with it.
        m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/doc",
            0,
            0,
            AnyData::Ref(r("/a")),
        ));
        assert_eq!(m.tree.nodes["/a1"].parent.as_deref(), Some("/b"));
        assert_consistent(&m);
    }

    #[test]
    fn the_moves_own_signals_after_the_walk_leave_the_object_where_it_went() {
        let mut m = mirror();
        m.admit_subtree(walked(sub(
            "/b",
            &[("/b", None, &["/a1"]), ("/a1", Some("/b"), &[])],
        )));
        let removed = m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/a",
            0,
            0,
            AnyData::Ref(r("/a1")),
        ));
        assert!(removed.is_empty());
        assert_eq!(m.stats.children_remove_after_move, 1);
        let added = m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/b",
            0,
            0,
            AnyData::Ref(r("/a1")),
        ));
        assert!(added.is_empty());
        assert_eq!(m.tree.nodes["/b"].children, vec!["/a1"]);
        // A child list of the old parent fetched later agrees.
        assert!(m.reconcile_children("/a", &[]).is_empty());
        assert_eq!(m.tree.nodes["/a1"].parent.as_deref(), Some("/b"));
        assert_consistent(&m);
    }

    #[test]
    fn a_remove_from_a_parent_that_still_lists_a_moved_child_unlists_it() {
        let mut m = mirror();
        // A stale listing the mirror was left with: /a1 lives under /b.
        m.tree.nodes.get_mut("/a1").unwrap().parent = Some("/b".into());
        m.tree.nodes.get_mut("/b").unwrap().children = vec!["/a1".into()];
        m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/a",
            0,
            0,
            AnyData::Ref(r("/a1")),
        ));
        assert!(m.tree.nodes["/a"].children.is_empty());
        assert!(m.tree.nodes.contains_key("/a1"));
        m.tree.renumber_all();
        assert_consistent(&m);
    }

    #[test]
    fn a_walk_drops_what_it_no_longer_finds_at_any_depth() {
        let mut m = mirror();
        let w = walked(sub(
            "/doc",
            &[
                ("/doc", None, &["/a", "/b"]),
                ("/a", Some("/doc"), &[]),
                ("/b", Some("/doc"), &[]),
            ],
        ));
        m.admit_subtree(w);
        assert!(!m.tree.nodes.contains_key("/a1"));
        assert_eq!(m.tree.len(), 4);
        assert_eq!(m.stats.objects_dropped_with_subtrees, 1);
        assert_consistent(&m);
    }

    #[test]
    fn a_held_object_is_updated_by_its_cache_item_and_not_pooled() {
        let mut m = mirror();
        let mut it = item("/b", "/doc", 1, 0);
        it.name = "stale".into();
        m.apply_add_accessible(it);
        assert_eq!(m.tree.nodes["/b"].name.as_deref(), Some("stale"));
        assert_eq!(m.tree.nodes["/b"].role, Some(73));
        assert_eq!(m.pool_len(), 0);
        // So a later walk asks the object itself.
        assert!(m.pool().item("/b").is_none());
        assert_eq!(m.stats.add_accessible_updated_existing, 1);
    }

    #[test]
    fn pooled_items_go_with_their_objects() {
        let mut m = mirror();
        m.apply_add_accessible(item("/gone", "/b", 0, 0));
        m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/b",
            0,
            0,
            AnyData::Ref(r("/gone")),
        ));
        assert_eq!(m.pool_len(), 0);
        m.apply_add_accessible(item("/x", "/b", 0, 0));
        m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/b",
            0,
            0,
            AnyData::Ref(r("/x")),
        ));
        // Kept for the walk the admission asks for.
        assert_eq!(m.pool_len(), 1);
        m.forget("/x");
        assert_eq!(m.pool_len(), 0);
        m.apply_add_accessible(item("/y", "/b", 0, 0));
        m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/b",
            0,
            0,
            AnyData::Ref(r("/y")),
        ));
        m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/doc",
            1,
            0,
            AnyData::Ref(r("/b")),
        ));
        assert_eq!(m.pool_len(), 0);
    }

    #[test]
    fn unused_items_expire_and_the_pool_has_a_limit() {
        let mut m = Mirror::with_limits(
            mirror().tree,
            Limits {
                max_pooled: 2,
                pool_batches: 2,
                ..Limits::default()
            },
        );
        for p in ["/p1", "/p2", "/p3"] {
            m.apply_add_accessible(item(p, "/b", 0, 0));
        }
        assert_eq!(m.pool_len(), 2);
        assert_eq!(m.stats.add_accessible_pool_full, 1);
        m.start_batch();
        m.start_batch();
        assert_eq!(m.pool_len(), 2);
        m.start_batch();
        assert_eq!(m.pool_len(), 0);
        assert_eq!(m.stats.pooled_items_dropped, 2);
    }

    #[test]
    fn an_admitted_objects_item_is_not_used_after_its_batch() {
        let mut m = mirror();
        m.apply_add_accessible(item("/x", "/b", 0, 0));
        m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/b",
            0,
            0,
            AnyData::Ref(r("/x")),
        ));
        assert!(m.pool().item("/x").is_some());
        m.start_batch();
        assert!(m.pool().item("/x").is_none());
    }

    #[test]
    fn an_event_for_a_pooled_object_supersedes_its_item() {
        let mut m = mirror();
        m.apply_add_accessible(item("/new", "/b", 0, 0));
        m.apply_event(&ev(
            "PropertyChange",
            "accessible-parent",
            "/new",
            0,
            0,
            AnyData::Ref(r("/b")),
        ));
        assert_eq!(m.pool_len(), 1);
        let repairs = m.apply_event(&ev(
            "StateChanged",
            "focused",
            "/new",
            1,
            0,
            AnyData::Int(0),
        ));
        assert_eq!(repairs, vec![Repair::Locate("/new".into())]);
        assert_eq!(m.pool_len(), 0);
        assert_eq!(m.stats.pooled_items_superseded, 1);
    }

    #[test]
    fn only_the_applications_own_signals_count_and_their_kinds_are_bounded() {
        let mut m = Mirror::with_limits(
            mirror().tree,
            Limits {
                max_signal_kinds: 4,
                ..Limits::default()
            },
        );
        let mut foreign = ev(
            "PropertyChange",
            "accessible-name",
            "/b",
            0,
            0,
            AnyData::Str("Elsewhere".into()),
        );
        foreign.sender = ":1.99".into();
        assert!(m.apply_event(&foreign).is_empty());
        assert_eq!(m.tree.nodes["/b"].name.as_deref(), Some(""));
        assert!(m.stats.signals.is_empty());
        for i in 0..20 {
            m.apply_event(&ev(
                "StateChanged",
                &format!("made-up-{i}"),
                "/b",
                1,
                0,
                AnyData::Int(0),
            ));
        }
        assert_eq!(m.stats.signals.len(), 5);
        assert_eq!(m.stats.signals[OTHER_SIGNALS], 16);
    }

    #[test]
    fn a_removal_hint_for_another_application_is_ignored() {
        let mut m = mirror();
        m.apply_remove_accessible(&ObjRef {
            bus: ":1.99".into(),
            path: "/a".into(),
        });
        assert!(m.tree.nodes.contains_key("/a1"));
        assert_eq!(m.stats.remove_hints_for_unknown_objects, 1);
    }

    #[test]
    fn a_full_mirror_admits_nothing_more() {
        let mut m = Mirror::with_limits(
            mirror().tree,
            Limits {
                max_objects: 6,
                ..Limits::default()
            },
        );
        let add = |m: &mut Mirror, c: &str| {
            m.apply_event(&ev(
                "ChildrenChanged",
                "add",
                "/b",
                0,
                0,
                AnyData::Ref(r(c)),
            ))
        };
        assert_eq!(add(&mut m, "/x"), vec![Repair::FetchSubtree("/x".into())]);
        assert!(m.is_full());
        assert!(add(&mut m, "/y").is_empty());
        let repairs = m.reconcile_children("/a", &[r("/a1"), r("/z")]);
        assert!(repairs.is_empty());
        assert_eq!(m.tree.nodes["/a"].children, vec!["/a1"]);
        let w = walked(sub(
            "/x",
            &[("/x", None, &["/x1"]), ("/x1", Some("/x"), &[])],
        ));
        m.admit_subtree(w);
        assert!(!m.tree.nodes.contains_key("/x1"));
        assert_eq!(m.stats.objects_refused_mirror_full, 3);
        assert_eq!(m.tree.len(), 6);
        assert_consistent(&m);
    }

    #[test]
    fn a_state_is_cleared_as_well_as_set() {
        let mut m = mirror();
        m.apply_event(&ev("StateChanged", "focused", "/b", 1, 0, AnyData::Int(0)));
        m.apply_event(&ev("StateChanged", "focused", "/b", 0, 0, AnyData::Int(0)));
        assert_eq!(m.tree.nodes["/b"].states, Some([0, 0]));
    }

    #[test]
    fn a_role_change_is_applied() {
        let mut m = mirror();
        let repairs = m.apply_event(&ev(
            "PropertyChange",
            "accessible-role",
            "/b",
            0,
            0,
            AnyData::Int(88),
        ));
        assert_eq!(m.tree.nodes["/b"].role, Some(88));
        assert_eq!(repairs, vec![Repair::RefetchAttributes("/b".into())]);
    }

    #[test]
    fn an_addition_counts_the_new_child() {
        let mut m = mirror();
        m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/doc",
            2,
            0,
            AnyData::Ref(r("/c")),
        ));
        assert_eq!(m.tree.nodes["/doc"].child_count, 3);
        assert_eq!(m.tree.nodes["/doc"].children, vec!["/a", "/b", "/c"]);
        assert_consistent(&m);
    }

    #[test]
    fn a_child_added_that_the_mirror_has_elsewhere_refetches_the_parent() {
        let mut m = mirror();
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/b",
            0,
            0,
            AnyData::Ref(r("/a1")),
        ));
        assert_eq!(repairs, vec![Repair::RefetchChildren("/b".into())]);
        // Under the same parent it is already where it belongs.
        let repairs = m.apply_event(&ev(
            "ChildrenChanged",
            "add",
            "/a",
            0,
            0,
            AnyData::Ref(r("/a1")),
        ));
        assert!(repairs.is_empty());
        assert_eq!(m.stats.children_add_already_present, 2);
    }

    #[test]
    fn a_reconcile_that_moves_a_child_refreshes_where_it_left() {
        let mut m = mirror();
        let attrs = Some(vec![("posinset".to_owned(), "1".to_owned())]);
        m.tree.nodes.insert("/a2".into(), node(Some("/a"), &[]));
        m.tree
            .nodes
            .get_mut("/a")
            .unwrap()
            .children
            .push("/a2".into());
        m.tree.renumber_all();
        m.tree.nodes.get_mut("/a2").unwrap().attributes = attrs;
        let repairs = sorted(m.reconcile_children("/b", &[r("/a1")]));
        assert_eq!(repairs, vec![Repair::RefetchAttributes("/a2".into())]);
        assert_eq!(m.tree.nodes["/a2"].index_in_parent, 0);
        assert_consistent(&m);
    }

    #[test]
    fn a_reply_that_lists_a_child_twice_lists_it_once() {
        let mut m = mirror();
        m.reconcile_children("/doc", &[r("/a"), r("/b"), r("/a")]);
        assert_eq!(m.tree.nodes["/doc"].children, vec!["/a", "/b"]);
        assert_consistent(&m);
    }

    fn named(path: &str, name: &str) -> Event {
        ev(
            "PropertyChange",
            "accessible-name",
            path,
            0,
            0,
            AnyData::Str(name.into()),
        )
    }

    fn added(parent: &str, at: i32, child: &str) -> Event {
        ev(
            "ChildrenChanged",
            "add",
            parent,
            at,
            0,
            AnyData::Ref(r(child)),
        )
    }

    #[test]
    fn a_change_after_admission_is_not_undone_by_the_objects_item() {
        let mut m = mirror();
        let mut it = item("/x", "/b", 0, 0);
        it.name = "old".into();
        m.apply_add_accessible(it);
        m.apply_event(&added("/b", 0, "/x"));
        assert_eq!(m.tree.nodes["/x"].name.as_deref(), Some("old"));
        assert!(m.pool().item("/x").is_some());
        m.apply_event(&named("/x", "new"));
        assert_eq!(m.tree.nodes["/x"].name.as_deref(), Some("new"));
        // So the walk the admission asked for fetches the name itself.
        assert!(m.pool().item("/x").is_none());
        // The item's count of no children is as stale once a child is added.
        m.apply_add_accessible(item("/y", "/b", 1, 0));
        m.apply_event(&added("/b", 1, "/y"));
        m.apply_event(&added("/y", 0, "/y1"));
        assert!(m.pool().item("/y").is_none());
        assert_eq!(m.stats.pooled_items_superseded, 2);
    }

    #[test]
    fn a_repair_given_up_on_is_tried_afresh_after_an_event_from_its_object() {
        let mut m = Mirror::with_limits(
            mirror().tree,
            Limits {
                max_tries: 2,
                ..Limits::default()
            },
        );
        let walk = Repair::FetchSubtree("/b".into());
        for _ in 0..3 {
            m.retry_later(walk.clone());
            m.start_batch();
            m.due_retries();
        }
        assert_eq!(m.stats.retries_given_up, 1);
        assert_eq!(m.retries_pending(), 0);
        for _ in 0..5 {
            m.start_batch();
            assert!(m.due_retries().is_empty());
        }
        let parent_only = ev(
            "PropertyChange",
            "accessible-parent",
            "/b",
            0,
            0,
            AnyData::Ref(r("/doc")),
        );
        assert!(m.apply_event(&parent_only).is_empty());
        let repairs = sorted(m.apply_event(&named("/b", "x")));
        assert_eq!(
            repairs,
            vec![walk.clone(), Repair::RefetchAttributes("/b".into())]
        );
        assert_eq!(m.stats.retries_revived, 1);
        // Its tries start again from none.
        m.retry_later(walk.clone());
        m.retry_later(walk);
        assert_eq!(m.retries_pending(), 1);
        assert_eq!(m.stats.retries_given_up, 1);
    }

    #[test]
    fn a_repair_given_up_on_is_forgotten_once_it_works_or_its_object_goes() {
        let mut m = Mirror::with_limits(
            mirror().tree,
            Limits {
                max_tries: 0,
                ..Limits::default()
            },
        );
        m.retry_later(Repair::FetchSubtree("/b".into()));
        m.retry_later(Repair::RefetchText("/a1".into()));
        assert_eq!(m.stats.retries_given_up, 2);
        m.admit_subtree(walked(sub("/b", &[("/b", None, &[])])));
        assert!(
            !m.apply_event(&named("/b", "x"))
                .contains(&Repair::FetchSubtree("/b".into()))
        );
        m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/a",
            0,
            0,
            AnyData::Ref(r("/a1")),
        ));
        m.apply_event(&added("/b", 0, "/a1"));
        assert!(
            !m.apply_event(&named("/a1", "x"))
                .contains(&Repair::RefetchText("/a1".into()))
        );
        assert_eq!(m.stats.retries_revived, 0);
    }

    #[test]
    fn a_child_list_the_walk_did_not_take_leaves_the_object_marked() {
        let mut m = mirror();
        let mut w = walked(sub("/b", &[("/b", None, &["/x"]), ("/x", Some("/b"), &[])]));
        // The count is from the object's cache item; its list would have
        // taken the walk past its object limit.
        w.tree.nodes.get_mut("/x").unwrap().child_count = 4;
        w.failed.insert(
            "/x".into(),
            [(Field::Children, FailureKind::OverLimit)].into(),
        );
        w.truncated = true;
        m.admit_subtree(w);
        let x = &m.tree.nodes["/x"];
        assert!(x.children.is_empty());
        assert!(x.children_unknown);
        assert_eq!(x.child_count, 4);
        assert!(!m.tree.nodes["/b"].children_unknown);
        assert_consistent(&m);
        // A list fetched in full clears the mark.
        m.admit_subtree(walked(sub(
            "/x",
            &[("/x", None, &["/x1"]), ("/x1", Some("/x"), &[])],
        )));
        assert!(!m.tree.nodes["/x"].children_unknown);
        assert_eq!(m.tree.nodes["/x"].child_count, 1);
    }

    #[test]
    fn a_list_not_known_keeps_the_reported_count_when_a_child_moves_out() {
        let mut m = mirror();
        let a = m.tree.nodes.get_mut("/a").unwrap();
        (a.children_unknown, a.child_count) = (true, 3);
        // A stale listing, which was never one of the application's.
        a.children.push("/b".into());
        let mut w = walked(sub(
            "/doc",
            &[
                ("/doc", None, &["/a", "/b"]),
                ("/a", Some("/doc"), &[]),
                ("/b", Some("/doc"), &["/a1"]),
                ("/a1", Some("/b"), &[]),
            ],
        ));
        w.failed
            .insert("/a".into(), [(Field::Children, timeout())].into());
        m.admit_subtree(w);
        let a = &m.tree.nodes["/a"];
        assert!(a.children.is_empty());
        assert!(a.children_unknown);
        assert_eq!(a.child_count, 2);
        assert_eq!(m.tree.nodes["/a1"].parent.as_deref(), Some("/b"));
        assert_consistent(&m);
        m.apply_event(&added("/a", 0, "/n"));
        assert_eq!(m.tree.nodes["/a"].child_count, 3);
        m.apply_event(&ev(
            "ChildrenChanged",
            "remove",
            "/a",
            0,
            0,
            AnyData::Ref(r("/n")),
        ));
        assert_eq!(m.tree.nodes["/a"].child_count, 2);
        assert!(m.tree.nodes["/a"].children_unknown);
    }

    #[test]
    fn an_admitted_object_is_marked_until_its_walk_lands() {
        let mut m = mirror();
        m.apply_event(&added("/b", 0, "/x"));
        assert!(m.tree.nodes["/x"].children_unknown);
        m.admit_subtree(walked(sub("/x", &[("/x", None, &[])])));
        assert!(!m.tree.nodes["/x"].children_unknown);
        assert_eq!(m.tree.nodes["/x"].child_count, 0);
        m.reconcile_children("/b", &[r("/x"), r("/z")]);
        assert!(m.tree.nodes["/z"].children_unknown);
        assert!(!m.tree.nodes["/b"].children_unknown);
    }

    #[test]
    fn what_a_full_mirror_or_a_cycle_leaves_out_marks_the_list() {
        let mut m = Mirror::with_limits(
            mirror().tree,
            Limits {
                max_objects: 5,
                ..Limits::default()
            },
        );
        m.apply_event(&added("/b", 0, "/x"));
        assert!(m.tree.nodes["/b"].children_unknown);
        let repairs = m.reconcile_children("/a", &[r("/a1"), r("/z")]);
        assert!(repairs.is_empty());
        let a = &m.tree.nodes["/a"];
        assert!(a.children_unknown);
        assert_eq!((a.children.len(), a.child_count), (1, 2));
        m.reconcile_children("/a", &[r("/a1")]);
        assert!(!m.tree.nodes["/a"].children_unknown);
        m.admit_subtree(walked(sub(
            "/a",
            &[
                ("/a", None, &["/a1", "/n"]),
                ("/a1", Some("/a"), &[]),
                ("/n", Some("/a"), &[]),
            ],
        )));
        assert!(m.tree.nodes["/a"].children_unknown);
        let returned = returns(|| {
            let mut m = mirror();
            m.reconcile_children("/b", &[r("/doc")]);
            let b = m.tree.nodes["/b"].children_unknown;
            m.admit_subtree(walked(sub(
                "/a1",
                &[("/a1", None, &["/a"]), ("/a", Some("/a1"), &[])],
            )));
            (b, m.tree.nodes["/a1"].children_unknown)
        });
        assert_eq!(returned, (true, true));
    }

    #[test]
    fn a_walk_keeps_an_object_only_listed_under_its_root_from_before_a_move() {
        let mut m = mirror();
        m.tree.nodes.get_mut("/b").unwrap().children = vec!["/a1".into()];
        m.admit_subtree(walked(sub("/b", &[("/b", None, &[])])));
        assert_eq!(m.tree.nodes["/a1"].parent.as_deref(), Some("/a"));
        assert_eq!(m.tree.nodes["/a"].children, vec!["/a1"]);
        assert!(m.tree.nodes["/b"].children.is_empty());
        assert_consistent(&m);
    }
}
