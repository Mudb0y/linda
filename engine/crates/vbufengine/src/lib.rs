//! The virtual buffer's renderer: walks a web document over a peer-to-peer connection to its
//! application and writes it as the render stream libnvdaVbuf builds NVDA's buffer storage from.
//!
//! The stream is UTF-16 code units, as NVDA's buffer offsets are on Windows:
//!   C<docHandle>,<ID>,<flags>,<n>;  opens a control field, followed by n pairs of name and value;
//!                                   flags 1 is isBlock, 2 isHidden
//!   T<text><n>;                     a text field in the open control field, followed by n pairs
//!   E                               closes the open control field
//! where a string is <length in units>:<units>.
//!
//! It is loaded into NVDA's process and called through ctypes, which releases the GIL; nothing here
//! touches Python. Connections are kept per application between calls.

#[allow(dead_code)]
mod tables;

use std::collections::{HashMap, HashSet};
use std::ffi::{CStr, CString, c_char};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use mirror::client::Client;
use mirror::consts::{IFACE_ACCESSIBLE, INTERFACE_TEXT_BIT, STATE_BUSY, interface_bits};
use mirror::decode;
use mirror::discovery::{self, Timeouts};
use mirror::fetch::{self, WalkFields, WalkLimits};
use mirror::tree::Tree;
use mirror::{Error, app};
use tables::*;

static CLIENTS: Mutex<Option<HashMap<String, Client>>> = Mutex::new(None);

const EMBEDDED_OBJECT: char = '\u{fffc}';
/// Calls in flight while walking: four already keep Firefox's main thread busy.
const WINDOW: usize = 4;
const IFACE_DOCUMENT: &str = "org.a11y.atspi.Document";

fn timeouts() -> Timeouts {
    Timeouts {
        method: Duration::from_secs(10),
        connect: Duration::from_secs(5),
    }
}

async fn client_for(bus_name: &str, stats: &mut serde_json::Map<String, serde_json::Value>) -> Result<Client, Error> {
    if let Some(c) = CLIENTS.lock().unwrap().as_ref().and_then(|m| m.get(bus_name)) {
        stats.insert("connectSeconds".into(), 0.0.into());
        return Ok(Client::p2p(c_conn(c), bus_name));
    }
    let t = Instant::now();
    let address = discovery::find_bus_address(timeouts()).await?;
    let bus = discovery::connect_bus(&address.address, timeouts()).await?;
    let owner = app::owner(&bus, bus_name).await?;
    let p2p_address = app::activate(&Client::bus(bus.clone(), bus_name)).await?;
    let conn = discovery::connect_p2p(&p2p_address, owner, timeouts()).await?;
    let client = Client::p2p(conn, bus_name);
    stats.insert("connectSeconds".into(), t.elapsed().as_secs_f64().into());
    CLIENTS
        .lock()
        .unwrap()
        .get_or_insert_with(HashMap::new)
        .insert(bus_name.to_owned(), Client::p2p(c_conn(&client), bus_name));
    Ok(client)
}

fn c_conn(c: &Client) -> zbus::Connection {
    c.conn.clone()
}

async fn prime_attributes(client: &Client, path: &str) -> usize {
    for attempt in 0..20 {
        if let Ok(msg) = client.call(path, IFACE_ACCESSIBLE, "GetAttributes", &()).await {
            if decode::attributes(&msg).is_ok_and(|a| !a.is_empty()) {
                return attempt + 1;
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    20
}

struct Renderer<'t> {
    tree: &'t Tree,
    doc_handle: i32,
    out: Vec<u16>,
    seen: HashSet<String>,
    nodes: usize,
    other_ids: HashMap<String, i32>,
    table_bit: u32,
}

fn push(out: &mut Vec<u16>, s: &str) {
    out.extend(s.encode_utf16());
}

fn push_string(out: &mut Vec<u16>, s: &str) {
    let units: Vec<u16> = s.encode_utf16().filter(|u| *u != 0).collect();
    push(out, &units.len().to_string());
    out.push(b':' as u16);
    out.extend(units);
}

fn has(states: [u32; 2], bit: u32) -> bool {
    states[(bit / 32) as usize] & (1 << (bit % 32)) != 0
}

fn strip_filler(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut last_astral = false;
    for c in text.chars() {
        if c == '\u{feff}' && last_astral {
            last_astral = false;
            continue;
        }
        last_astral = c as u32 > 0xffff;
        out.push(c);
    }
    out
}

impl Renderer<'_> {
    /// The number at the end of the path, cast to signed 32 bits as virtualBuffers.atspi does: the
    /// ATK bridge counts up from 1 and never reuses one, Qt counts from 2^31.
    fn id(&mut self, path: &str) -> i32 {
        if let Some(tail) = path.rsplit('/').next() {
            if let Ok(n) = tail.parse::<u32>() {
                return n as i32;
            }
        }
        let next = -(self.other_ids.len() as i32 + 1);
        *self.other_ids.entry(path.to_owned()).or_insert(next)
    }

    fn states(&self, raw: [u32; 2], role: u32, attrs: &[(String, String)]) -> Vec<u64> {
        let mut s: Vec<u64> = NVDA_STATES
            .iter()
            .filter(|(bit, _)| has(raw, *bit))
            .map(|(_, v)| *v)
            .collect();
        if !has(raw, ATSPI_SENSITIVE) && !has(raw, ATSPI_ENABLED) && role != ROLE_ICON {
            s.push(STATE_UNAVAILABLE);
        }
        if !has(raw, ATSPI_VISIBLE) {
            s.push(STATE_INVISIBLE);
        } else if !has(raw, ATSPI_SHOWING) {
            s.push(STATE_OFFSCREEN);
        }
        if has(raw, ATSPI_EXPANDABLE) && !has(raw, ATSPI_EXPANDED) {
            s.push(STATE_COLLAPSED);
        }
        if has(raw, ATSPI_INDETERMINATE) {
            if role == ROLE_CHECKBOX || role == ROLE_CHECKMENUITEM {
                s.push(STATE_HALFCHECKED);
            } else {
                s.push(STATE_INDETERMINATE);
            }
        }
        if role == ROLE_LINK {
            s.push(STATE_LINKED);
        }
        if role != ROLE_LIST && role != ROLE_LISTITEM && s.contains(&STATE_READONLY) {
            s.retain(|v| *v != STATE_EDITABLE);
        }
        if s.contains(&STATE_HALFCHECKED) {
            s.retain(|v| *v != STATE_CHECKED);
        }
        let _ = attrs;
        s.sort_unstable();
        s.dedup();
        s
    }

    fn node(&mut self, path: &str, table_id: Option<i32>) {
        let tree = self.tree;
        let Some(n) = tree.nodes.get(path) else { return };
        if !self.seen.insert(path.to_owned()) {
            return;
        }
        self.nodes += 1;
        let id = self.id(path);
        let raw_role = n.role.unwrap_or(0);
        let raw_name = ROLE_NAMES.get(raw_role as usize).copied().unwrap_or("");
        let raw = n.states.unwrap_or([0, 0]);
        let empty = Vec::new();
        let attrs: &Vec<(String, String)> = n.attributes.as_ref().unwrap_or(&empty);
        let attr = |k: &str| attrs.iter().find(|(a, _)| a == k).map(|(_, v)| v.as_str());
        let ifaces = n.interfaces.unwrap_or(0);
        let name = n.name.as_deref().unwrap_or("");
        let mut role = NVDA_ROLES.get(raw_role as usize).copied().unwrap_or(ROLE_UNKNOWN);
        if raw_name == "text" || raw_name == "static" {
            role = if has(raw, ATSPI_EDITABLE) { ROLE_EDITABLETEXT } else { ROLE_STATICTEXT };
        }
        let states = self.states(raw, role, attrs);
        let is_editable = has(raw, ATSPI_EDITABLE) && role != ROLE_DOCUMENT;
        let is_block = if has(raw, ATSPI_MULTI_LINE) {
            true
        } else if let Some(d) = attr("display") {
            !matches!(d, "inline" | "inline-block" | "inline-flex")
        } else {
            matches!(
                role,
                ROLE_TABLE | ROLE_TABLECELL | ROLE_SECTION | ROLE_DOCUMENT | ROLE_INTERNALFRAME | ROLE_UNKNOWN | ROLE_SEPARATOR
            )
        };
        let name_is_content = matches!(
            role,
            ROLE_LINK | ROLE_BUTTON | ROLE_TOGGLEBUTTON | ROLE_MENUITEM | ROLE_GRAPHIC | ROLE_HEADING | ROLE_TAB | ROLE_MENUBUTTON | ROLE_STATICTEXT
        );
        let mut field: Vec<(String, String)> = vec![
            ("role".into(), role.to_string()),
            ("atspi::role".into(), raw_name.to_owned()),
        ];
        for s in &states {
            field.push((format!("state_{s}"), "1".into()));
        }
        for key in ["level", "xml-roles", "tag", "display", "explicit-name", "posinset", "setsize"] {
            if let Some(v) = attr(key) {
                field.push((key.into(), v.into()));
            }
        }
        if !name.is_empty() && !name_is_content {
            field.push(("name".into(), name.into()));
        }
        let mut table_id = table_id;
        if ifaces & self.table_bit != 0 && table_id.is_none() {
            // Row and column counts and cell coordinates are left out: each costs a call.
            table_id = Some(id);
            field.push(("table-id".into(), id.to_string()));
        }
        let out = &mut self.out;
        out.push(b'C' as u16);
        push(out, &format!("{},{},{},{};", self.doc_handle, id, is_block as u8, field.len()));
        for (k, v) in &field {
            push_string(out, k);
            push_string(out, v);
        }
        let mark = self.out.len();
        let selection_container = matches!(raw_name, "list box" | "tree" | "tree table") && !states.contains(&STATE_READONLY);
        let render_children = role != ROLE_COMBOBOX && !selection_container;
        let text = if ifaces & INTERFACE_TEXT_BIT != 0 { n.text.as_deref().unwrap_or("") } else { "" };
        let children: Vec<String> = n.children.clone();
        if render_children && !text.is_empty() && (is_editable || !text.chars().all(char::is_whitespace)) {
            let text = strip_filler(text);
            let mut index = 0usize;
            let mut chunk = String::new();
            for c in text.chars() {
                if c != EMBEDDED_OBJECT {
                    chunk.push(c);
                    continue;
                }
                if !chunk.is_empty() {
                    self.out.push(b'T' as u16);
                    push_string(&mut self.out, &chunk);
                    push(&mut self.out, "0;");
                    chunk.clear();
                }
                if let Some(child) = children.get(index) {
                    self.node(child, table_id);
                }
                index += 1;
            }
            if !chunk.is_empty() {
                self.out.push(b'T' as u16);
                push_string(&mut self.out, &chunk);
                push(&mut self.out, "0;");
            }
        } else if render_children && text.is_empty() {
            for child in &children {
                self.node(child, table_id);
            }
        } else if role == ROLE_COMBOBOX {
            if let Some(value) = self.combo_value(&children) {
                self.out.push(b'T' as u16);
                push_string(&mut self.out, &value);
                push(&mut self.out, "0;");
            }
        }
        let mut has_content = self.out.len() > mark;
        if !has_content && !name.is_empty() && (name_is_content || role == ROLE_SECTION || role == ROLE_TEXTFRAME) {
            self.out.push(b'T' as u16);
            push_string(&mut self.out, name);
            push(&mut self.out, "0;");
            has_content = true;
        }
        if !has_content
            && (matches!(role, ROLE_TABLECELL | ROLE_TABLEROWHEADER | ROLE_TABLECOLUMNHEADER | ROLE_SEPARATOR | ROLE_GRAPHIC)
                || states.contains(&STATE_FOCUSABLE)
                || !name.is_empty())
        {
            self.out.push(b'T' as u16);
            push_string(&mut self.out, " ");
            push(&mut self.out, "0;");
        }
        self.out.push(b'E' as u16);
    }

    fn combo_value(&self, children: &[String]) -> Option<String> {
        for c in children {
            let Some(n) = self.tree.nodes.get(c) else { continue };
            for item in &n.children {
                if let Some(i) = self.tree.nodes.get(item) {
                    if i.states.is_some_and(|s| has(s, 23)) {
                        return i.name.clone();
                    }
                }
            }
        }
        None
    }
}

async fn render(
    bus_name: &str,
    doc_path: &str,
    doc_handle: i32,
    stats: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<Vec<u16>, Error> {
    let total = Instant::now();
    let client = client_for(bus_name, stats).await?;
    // GetItems lists only what the bridge has handed out: a document loaded after the application
    // was activated, which on a running desktop is every document, is missing from it. So the
    // document is walked, WINDOW calls in flight, every field fetched.
    let t = Instant::now();
    let primes = prime_attributes(&client, doc_path).await;
    stats.insert("attributePrimeCalls".into(), primes.into());
    let walked = fetch::walk(
        &client,
        doc_path,
        WINDOW,
        WalkFields::ALL,
        None,
        WalkLimits {
            max_objects: 500_000,
            max_time: Duration::from_secs(120),
        },
    )
    .await;
    stats.insert("walkSeconds".into(), t.elapsed().as_secs_f64().into());
    stats.insert("walkErrors".into(), walked.errors.values().sum::<usize>().into());
    stats.insert("walkTruncated".into(), walked.truncated.into());
    let doc = walked.tree;
    stats.insert("documentObjects".into(), doc.len().into());
    let busy = doc.nodes.get(doc_path).and_then(|n| n.states).is_some_and(|s| has(s, STATE_BUSY));
    stats.insert("busy".into(), busy.into());
    if let Ok(msg) = client.call(doc_path, IFACE_DOCUMENT, "GetAttributes", &()).await {
        let attributes = decode::attributes(&msg).unwrap_or_default();
        if let Some((_, url)) = attributes.into_iter().find(|(k, _)| k == "DocURL") {
            stats.insert("docURL".into(), url.into());
        }
    }
    stats.insert("calls".into(), client.counters().calls.into());
    let t = Instant::now();
    let mut r = Renderer {
        tree: &doc,
        doc_handle,
        out: Vec::with_capacity(1 << 20),
        seen: HashSet::new(),
        nodes: 0,
        other_ids: HashMap::new(),
        table_bit: interface_bits(["org.a11y.atspi.Table"]),
    };
    r.node(doc_path, None);
    stats.insert("renderSeconds".into(), t.elapsed().as_secs_f64().into());
    stats.insert("objects".into(), r.nodes.into());
    stats.insert("complete".into(), true.into());
    stats.insert("seconds".into(), total.elapsed().as_secs_f64().into());
    Ok(r.out)
}

/// Renders the document at doc_path in the application bus_name. Blocks; never call it on NVDA's main
/// thread. On success returns 1, with the stream and its length in UTF-16 units in *stream and
/// *length, freed with bre_vbuf_free_stream. Returns 0 on failure, with *stream null. Either way
/// *info is a JSON object freed with bre_vbuf_free_str: complete, busy, docURL, objects, seconds and
/// timings, or error. No flag is defined yet.
///
/// # Safety
/// bus_name and doc_path are NUL-terminated; stream, length and info are writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_render(
    bus_name: *const c_char,
    doc_path: *const c_char,
    doc_handle: i32,
    flags: u32,
    stream: *mut *mut u16,
    length: *mut usize,
    info: *mut *mut c_char,
) -> i32 {
    let _ = flags;
    let bus_name = unsafe { CStr::from_ptr(bus_name) }.to_string_lossy().into_owned();
    let doc_path = unsafe { CStr::from_ptr(doc_path) }.to_string_lossy().into_owned();
    let mut s = serde_json::Map::new();
    let r = zbus::block_on(render(&bus_name, &doc_path, doc_handle, &mut s));
    let ok = match r {
        Ok(v) => {
            let b = v.into_boxed_slice();
            unsafe {
                *length = b.len();
                *stream = Box::into_raw(b) as *mut u16;
            }
            1
        }
        Err(e) => {
            s.insert("error".into(), e.to_string().into());
            unsafe {
                *length = 0;
                *stream = std::ptr::null_mut();
            }
            0
        }
    };
    let json = serde_json::Value::Object(s).to_string();
    unsafe { *info = CString::new(json).unwrap_or_default().into_raw() };
    ok
}

/// # Safety
/// stream and length are what bre_vbuf_render gave.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_free_stream(stream: *mut u16, length: usize) {
    if !stream.is_null() {
        drop(unsafe { Box::from_raw(std::ptr::slice_from_raw_parts_mut(stream, length)) });
    }
}

/// # Safety
/// s is what bre_vbuf_render stored in *info.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_free_str(s: *mut c_char) {
    if !s.is_null() {
        drop(unsafe { CString::from_raw(s) });
    }
}
