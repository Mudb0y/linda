//! Writing objects as the render stream libnvdaVbuf builds NVDA's buffer from, by the rules of
//! nvdaHelper/vbufBackends/gecko_ia2/gecko_ia2.cpp fillVBuf, which NVDA uses for Firefox on Windows.
//!
//! The stream is UTF-16 code units:
//!   C<docHandle>,<ID>,<flags>,<n>;  opens a control field, followed by n pairs of name and value;
//!                                   flags 1 is isBlock, 2 isHidden
//!   T<text><n>;                     a text field in the open control field, followed by n pairs
//!   E                               closes the open control field
//!   S<docHandle>,<ID>;              in an update: the next field replaces the one with this identifier
//!   R<docHandle>,<ID>;              in an update: the field with this identifier, kept as it is
//! where a string is <length in units>:<units>.

use std::collections::{HashMap, HashSet};

use crate::tables::*;
use crate::walk::{Obj, has, iface_bit};

const EMBEDDED_OBJECT: char = '\u{fffc}';
const EMPTY_TEXT: &str = " ";
const FLAG_BLOCK: u16 = 1;
/// Fields nested deeper than this are written flat, one after another inside the field at this
/// depth, in the order they come. Each level of nesting is a level of recursion here, on the
/// engine's thread, and in the buffer's storage on NVDA's threads, so a page nested deeper would
/// overflow a stack and take the whole reader down. No page nests a thousand levels for its
/// reader's sake.
pub const MAX_DEPTH: usize = 1000;

/// What an object's ancestors decide about how it is rendered.
#[derive(Debug, Clone, Copy, Default)]
pub struct Ctx {
    /// The table being rendered, until a cell of it is reached.
    pub table_id: Option<i32>,
    pub in_link: bool,
    pub ignore_unlabelled_graphics: bool,
    /// An ancestor reads its name as content, so nothing under it is kept on a re-render.
    pub no_reuse: bool,
}

/// The number at the end of the object path, cast to signed 32 bits as virtualBuffers.atspi does:
/// the ATK bridge counts up from 1 and never reuses one, Qt counts from 2^31. A path ending in
/// anything else but an application's root is given a number by [`crate::named`]; the root is
/// given no field.
pub fn id_of(path: &str) -> Option<i32> {
    let tail = path.rsplit('/').next()?;
    if let Ok(n) = tail.parse::<u32>() {
        return Some(n as i32);
    }
    (!tail.is_empty() && tail != "root").then(|| crate::named::id(path))
}

/// Gecko fills the second unit of each astral character with U+FEFF, so that its text has as many
/// characters as UTF-16 units; dropping the filler lines its offsets up with the buffer's again.
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

/// utils.cpp getNameForURL: the last part of an address's path without its extension, with its
/// query and fragment, at most 30 characters.
pub fn name_for_url(url: &str) -> String {
    let url: Vec<char> = url.chars().collect();
    if url.is_empty() {
        return String::new();
    }
    let find = |c: char| url.iter().position(|x| *x == c);
    let rfind = |c: char, before: usize| url[..before.min(url.len())].iter().rposition(|x| *x == c);
    let colon = find(':');
    if let Some(cp) = colon
        && url[cp..].iter().take(3).collect::<String>() != "://"
    {
        let lower: String = url.iter().take(11).collect::<String>().to_lowercase();
        if lower == "data:image/" {
            return String::new();
        }
        return url[cp + 1..].iter().collect();
    }
    let query = rfind('?', url.len());
    let anchor = rfind('#', url.len());
    let mut strip_extension = true;
    let path_end_excl = query.or(anchor).unwrap_or(url.len());
    let mut end: Option<usize> = path_end_excl.checked_sub(1);
    if let Some(e) = end
        && url[e] == '/'
    {
        end = e.checked_sub(1);
        strip_extension = false;
    }
    let mut start = 0;
    if let Some(e) = end {
        match rfind('/', e + 1) {
            None => start = 0,
            Some(s) => {
                start = s + 1;
                if strip_extension && colon.is_some_and(|cp| start == cp + 3) {
                    strip_extension = false;
                }
            }
        }
        if strip_extension
            && let Some(dot) = rfind('.', e + 1)
            && dot > start
        {
            end = Some(dot - 1);
        }
    }
    let mut name: String = match end {
        Some(e) if e + 1 >= start => url[start..=e].iter().collect(),
        _ => String::new(),
    };
    if let Some(q) = query {
        let stop = anchor.filter(|a| *a > q).unwrap_or(url.len());
        name.push(' ');
        name.extend(&url[q + 1..stop]);
    }
    if let Some(a) = anchor {
        name.push(' ');
        name.extend(&url[a + 1..]);
    }
    if name.chars().count() > 30 {
        name = name.chars().take(30).collect::<String>() + "\u{2026}";
    }
    name
}

/// What rendering decided about each field written, for the next update to go by.
pub struct Mark {
    pub path: String,
    pub child_reuse: bool,
    pub escalate: bool,
    pub child_ctx: Ctx,
}

pub struct Renderer<'a> {
    objs: &'a HashMap<String, Obj>,
    doc_handle: i32,
    root: &'a str,
    /// Stop at the first object not yet fetched in full, for the top of a document still being
    /// walked.
    prefix_only: bool,
    /// Children written as R.
    reuse: &'a HashSet<String>,
    pub out: Vec<u16>,
    seen: HashSet<String>,
    pub stopped: bool,
    pub nodes: usize,
    pub marks: Vec<Mark>,
    /// Units of text written so far, and how many of them are neither space nor private use, for
    /// utils.cpp nodeHasUsefulContent.
    units: usize,
    useful_units: usize,
    /// The language of the object being written, from it or its nearest ancestor that has one,
    /// which each text field carries as gecko_ia2.cpp gives it from IA2 locale.
    language: Option<String>,
    /// Objects below MAX_DEPTH waiting to be written flat, with their context and language.
    deferred: Vec<(String, Ctx, Option<String>)>,
    flattening: bool,
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

fn nvda_states(raw: [u32; 2], role: u32) -> Vec<u64> {
    let mut s: Vec<u64> = NVDA_STATES
        .iter()
        .filter(|(b, _)| has(raw, *b))
        .map(|(_, v)| *v)
        .collect();
    if !has(raw, ATSPI_SENSITIVE) && !has(raw, ATSPI_ENABLED) && role != ROLE_ICON {
        s.push(STATE_UNAVAILABLE);
    }
    if !has(raw, ATSPI_VISIBLE) {
        s.push(STATE_INVISIBLE);
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
    s.sort_unstable();
    s.dedup();
    s
}

/// Object attributes NVDA reads from the buffer, copied as they are.
const COPIED_ATTRIBUTES: [&str; 14] = [
    "level",
    "xml-roles",
    "tag",
    "display",
    "explicit-name",
    "posinset",
    "setsize",
    "placeholder-text",
    "roledescription",
    "current",
    "haspopup",
    "autocomplete",
    "sort",
    "valuetext",
];

fn format_value(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

impl<'a> Renderer<'a> {
    pub fn new(
        objs: &'a HashMap<String, Obj>,
        doc_handle: i32,
        root: &'a str,
        prefix_only: bool,
        reuse: &'a HashSet<String>,
    ) -> Self {
        Renderer {
            objs,
            doc_handle,
            root,
            prefix_only,
            reuse,
            out: Vec::with_capacity(1 << 16),
            seen: HashSet::new(),
            stopped: false,
            nodes: 0,
            marks: Vec::new(),
            units: 0,
            useful_units: 0,
            language: None,
            deferred: Vec::new(),
            flattening: false,
        }
    }

    /// The language path's text is in when its own object does not say: its nearest ancestor's.
    fn language_for(&self, path: &str) -> Option<String> {
        let mut p = self.objs.get(path).and_then(|o| o.parent.clone());
        while let Some(here) = p {
            let o = self.objs.get(&here)?;
            if let Some(l) = o.locale.as_deref().and_then(crate::chromium::page_language) {
                return Some(l.to_owned());
            }
            p = o.parent.clone();
        }
        None
    }

    /// The context path's own children were rendered with: from its nearest rendered ancestor.
    pub fn ctx_for(&self, path: &str) -> Ctx {
        let mut p = self.objs.get(path).and_then(|o| o.parent.clone());
        while let Some(here) = p {
            let Some(o) = self.objs.get(&here) else { break };
            if o.rendered {
                return o.child_ctx;
            }
            p = o.parent.clone();
        }
        Ctx::default()
    }

    /// How many fields hold path's in the buffer, as far as the last render left them.
    fn depth_of(&self, path: &str) -> usize {
        let mut depth = 0;
        let mut p = self.objs.get(path).and_then(|o| o.parent.clone());
        while let Some(here) = p {
            let Some(o) = self.objs.get(&here) else { break };
            if o.rendered {
                depth += 1;
            }
            p = o.parent.clone();
        }
        depth.min(MAX_DEPTH)
    }

    /// Writes path's field, and everything in it.
    pub fn render(&mut self, path: &str, ctx: Ctx) {
        self.language = self.language_for(path);
        self.node(path, ctx, true, 0);
    }

    /// Writes an S, then path's field.
    pub fn replace(&mut self, path: &str) {
        let Some(id) = id_of(path) else { return };
        self.out.push(b'S' as u16);
        push(&mut self.out, &format!("{},{};", self.doc_handle, id));
        let ctx = self.ctx_for(path);
        self.language = self.language_for(path);
        let depth = self.depth_of(path);
        self.node(path, ctx, true, depth);
    }

    /// Writes a child at depth. Below MAX_DEPTH the child waits until the field above it closes, and
    /// is then written after it, so that nothing nests, and nothing recurses, deeper than one level
    /// below MAX_DEPTH.
    fn child(&mut self, path: &str, ctx: Ctx, depth: usize) {
        if depth > MAX_DEPTH + 1 {
            self.deferred
                .push((path.to_owned(), ctx, self.language.clone()));
            return;
        }
        self.node(path, ctx, false, depth);
        if depth == MAX_DEPTH + 1 && !self.flattening {
            self.flattening = true;
            let outer_language = self.language.clone();
            let mut stack: Vec<_> = self.deferred.drain(..).rev().collect();
            while let Some((path, ctx, language)) = stack.pop() {
                self.language = language;
                self.node(&path, ctx, false, depth);
                stack.extend(self.deferred.drain(..).rev());
            }
            self.language = outer_language;
            self.flattening = false;
        }
    }

    fn text(&mut self, s: &str) {
        if s.is_empty() || self.stopped {
            return;
        }
        self.out.push(b'T' as u16);
        push_string(&mut self.out, s);
        if let Some(language) = &self.language {
            push(&mut self.out, "1;");
            push_string(&mut self.out, "language");
            push_string(&mut self.out, language);
        } else {
            push(&mut self.out, "0;");
        }
        let n = s.encode_utf16().count();
        self.units += n;
        self.useful_units += s
            .chars()
            .filter(|c| !c.is_whitespace() && !('\u{e000}'..='\u{f8ff}').contains(c))
            .count();
    }

    fn node(&mut self, path: &str, ctx: Ctx, is_start: bool, depth: usize) {
        if self.stopped {
            return;
        }
        // A kept field brings its nesting with it, which a field written flat must not have.
        if !is_start && depth <= MAX_DEPTH && self.reuse.contains(path) {
            if let Some(id) = id_of(path) {
                self.out.push(b'R' as u16);
                push(&mut self.out, &format!("{},{};", self.doc_handle, id));
                // Kept content counts as useful: it was rendered from something.
                self.units += 4;
                self.useful_units += 1;
            }
            return;
        }
        let objs = self.objs;
        let Some(o) = objs.get(path) else { return };
        if self.prefix_only && !o.complete() {
            self.stopped = true;
            return;
        }
        if !self.seen.insert(path.to_owned()) {
            return;
        }
        let Some(id) = id_of(path) else {
            for c in &o.children {
                self.child(c, ctx, depth + 1);
            }
            return;
        };
        self.nodes += 1;
        let outer_language = self.language.clone();
        if let Some(l) = o.locale.as_deref().and_then(crate::chromium::page_language) {
            self.language = Some(l.to_owned());
        }
        let raw_name = ROLE_NAMES.get(o.role as usize).copied().unwrap_or("");
        let raw = o.states;
        let tag = o.attr("tag");
        let ifaces = o.interfaces;
        let full_text = if ifaces & iface_bit("org.a11y.atspi.Text") != 0 {
            strip_filler(o.text.as_deref().unwrap_or(""))
        } else {
            String::new()
        };
        let mut role = NVDA_ROLES
            .get(o.role as usize)
            .copied()
            .unwrap_or(ROLE_UNKNOWN);
        let mut name: String = o.name.clone();
        let mut text: &str = &full_text;
        if raw_name == "static" && tag == Some("img") {
            // An image whose picture did not load: Firefox gives its alt text as text.
            role = ROLE_GRAPHIC;
            if name.is_empty() {
                name = full_text.trim().to_owned();
            }
            text = "";
        } else if raw_name == "static"
            && (tag.is_some() || !o.children.is_empty())
            && !has(raw, ATSPI_EDITABLE)
        {
            // Inline HTML elements (span, abbr, b, code, ...) have ATK_ROLE_STATIC in Gecko where
            // IAccessible2 gives them IA2_ROLE_TEXT_FRAME: their children and text are content
            // rather than replaced by an explicit name such as a title attribute.
            role = ROLE_TEXTFRAME;
        } else if raw_name == "text" || raw_name == "static" {
            role = if has(raw, ATSPI_EDITABLE) {
                ROLE_EDITABLETEXT
            } else {
                ROLE_STATICTEXT
            };
        }
        let states = nvda_states(raw, role);
        let is_root = path == self.root;
        let is_editable = has(raw, ATSPI_EDITABLE) && role != ROLE_DOCUMENT;
        let in_link = ctx.in_link || role == ROLE_LINK;
        let embedded_app = raw_name == "embedded"
            || (!is_root && (raw_name == "application" || raw_name == "dialog"));
        let unavailable = states.contains(&STATE_UNAVAILABLE);
        let never_interactive =
            !is_editable && (is_root || role == ROLE_DOCUMENT || role == ROLE_INTERNALFRAME);
        let mut interactive = !never_interactive
            && (is_editable
                || in_link
                || states.contains(&STATE_FOCUSABLE)
                || unavailable
                || embedded_app);
        let name_explicit = o.attr("explicit-name") == Some("true");
        let img_map = role == ROLE_GRAPHIC && !o.children.is_empty();
        let name_is_content = embedded_app
            || matches!(
                role,
                ROLE_LINK
                    | ROLE_BUTTON
                    | ROLE_TOGGLEBUTTON
                    | ROLE_MENUITEM
                    | ROLE_HEADING
                    | ROLE_TAB
                    | ROLE_MENUBUTTON
            )
            || (role == ROLE_GRAPHIC && !img_map)
            || (role == ROLE_STATICTEXT && !is_editable)
            || ((role == ROLE_CHECKBOX || role == ROLE_RADIOBUTTON) && !o.label_visible);
        let label_visible = name_explicit
            && !name.is_empty()
            && (!name_is_content || role == ROLE_TABLE)
            && o.label_visible;

        let is_block = if has(raw, ATSPI_MULTI_LINE) {
            true
        } else if let Some(d) = o.attr("display") {
            !matches!(d, "inline" | "inline-block" | "inline-flex")
        } else {
            matches!(
                role,
                ROLE_TABLE
                    | ROLE_TABLECELL
                    | ROLE_SECTION
                    | ROLE_DOCUMENT
                    | ROLE_INTERNALFRAME
                    | ROLE_UNKNOWN
                    | ROLE_SEPARATOR
            )
        };

        let mut field: Vec<(String, String)> = vec![
            ("role".into(), role.to_string()),
            ("atspi::role".into(), raw_name.to_owned()),
        ];
        for s in &states {
            field.push((format!("state_{s}"), "1".into()));
        }
        for key in COPIED_ATTRIBUTES {
            if let Some(v) = o.attr(key) {
                field.push((key.into(), v.into()));
            }
        }
        if !name.is_empty() && !name_is_content {
            field.push(("name".into(), name.clone()));
        }
        if !o.description.is_empty() {
            field.push(("description".into(), o.description.clone()));
        }
        if name_explicit && !name_is_content && role != ROLE_TABLE && !label_visible {
            field.push(("alwaysReportName".into(), "true".into()));
        }
        if o.action.as_deref() == Some("click") {
            field.push(("atspi::action_click".into(), "1".into()));
        }
        if o.has_details {
            field.push(("hasDetails".into(), "true".into()));
        }
        if let Some(message) = o.error_targets.iter().find_map(|t| self.summary(t)) {
            field.push(("errorMessage".into(), message));
        }

        let mut child_ctx = ctx;
        child_ctx.in_link = in_link;
        child_ctx.no_reuse = ctx.no_reuse || name_is_content;
        let mut table_summary: Option<String> = None;
        let in_table = ctx.table_id.is_some();
        if let Some([row, column, row_span, column_span]) = o.cell {
            field.push(("table-rownumber".into(), (row + 1).to_string()));
            field.push(("table-columnnumber".into(), (column + 1).to_string()));
            if column_span > 1 {
                field.push(("table-columnsspanned".into(), column_span.to_string()));
            }
            if row_span > 1 {
                field.push(("table-rowsspanned".into(), row_span.to_string()));
            }
            for (key, headers) in [
                ("table-columnheadercells", &o.column_headers),
                ("table-rowheadercells", &o.row_headers),
            ] {
                let mut s = String::new();
                for h in headers.iter() {
                    if let Some(hid) = id_of(h) {
                        s.push_str(&format!("{},{};", self.doc_handle, hid));
                    }
                }
                if !s.is_empty() {
                    field.push((key.into(), s));
                }
            }
            field.push(("table-id".into(), ctx.table_id.unwrap_or(0).to_string()));
            child_ctx.table_id = None;
        } else if let (Some((rows, columns)), false) = (o.table, in_table) {
            if o.attr("layout-guess") == Some("true") {
                field.push(("table-layout".into(), "1".into()));
            }
            field.push(("table-id".into(), id.to_string()));
            field.push(("table-rowcount".into(), rows.to_string()));
            field.push(("table-columncount".into(), columns.to_string()));
            if rows > 0 || columns > 0 {
                child_ctx.table_id = Some(id);
            }
            if !o.description.is_empty() {
                table_summary = Some(o.description.clone());
            } else if !name.is_empty() && !label_visible {
                table_summary = Some(name.clone());
            }
        }
        for (attr, key) in [
            ("rowindex", "table-rownumber-presentational"),
            ("colindex", "table-columnnumber-presentational"),
            ("rowcount", "table-rowcount-presentational"),
            ("colcount", "table-columncount-presentational"),
        ] {
            if let Some(v) = o.attr(attr) {
                field.push((key.into(), v.into()));
            }
        }

        let out = &mut self.out;
        out.push(b'C' as u16);
        push(out, &format!("{},{},", self.doc_handle, id));
        let flags_at = out.len();
        push(
            out,
            &format!("{},{};", if is_block { FLAG_BLOCK } else { 0 }, field.len()),
        );
        for (k, v) in &field {
            push_string(out, k);
            push_string(out, v);
        }
        let start_units = self.units;
        let start_useful = self.useful_units;
        let content_at = self.out.len();
        if let Some(summary) = table_summary {
            self.text(&summary);
        }

        let render_selected_only = (raw_name == "list box" && !states.contains(&STATE_READONLY))
            || raw_name == "tree"
            || raw_name == "tree table";
        let text_unneeded_space = !text.is_empty()
            && !is_editable
            && text
                .chars()
                .all(|c| c != '\n' && c != EMBEDDED_OBJECT && c.is_whitespace());
        let render_children = !(text_unneeded_space
            || (role == ROLE_COMBOBOX && !is_editable)
            || render_selected_only
            || embedded_app
            || (name_is_content && name_explicit));
        if interactive && !child_ctx.ignore_unlabelled_graphics {
            child_ctx.ignore_unlabelled_graphics = !name.is_empty();
        }
        let mut selected_child: Option<String> = None;
        if render_children && !text.is_empty() {
            let embedded = text.chars().filter(|c| *c == EMBEDDED_OBJECT).count();
            let embedded = crate::chromium::embedded_children(o, objs, embedded);
            let mut index = 0usize;
            let mut chunk = String::new();
            for c in text.chars() {
                if self.stopped {
                    break;
                }
                if c != EMBEDDED_OBJECT {
                    chunk.push(c);
                    continue;
                }
                let done = std::mem::take(&mut chunk);
                self.text(&done);
                if let Some(child) = embedded.get(index) {
                    self.child(child, child_ctx, depth + 1);
                }
                index += 1;
            }
            self.text(&chunk);
        } else if render_children && text.is_empty() && !o.children.is_empty() {
            for child in &o.children {
                self.child(child, child_ctx, depth + 1);
            }
        } else if render_selected_only {
            let selected = o
                .children
                .iter()
                .find(|c| objs.get(*c).is_some_and(|k| has(k.states, ATSPI_SELECTED)))
                .or(o.children.first());
            if let Some(c) = selected {
                selected_child = Some(c.clone());
                self.child(c, child_ctx, depth + 1);
            }
        } else if role == ROLE_COMBOBOX {
            if let Some(v) = self.combo_value(o) {
                self.text(&v);
            }
        } else if role == ROLE_GRAPHIC {
            if !name.is_empty() {
                self.text(&name);
            } else if ctx.ignore_unlabelled_graphics {
                interactive = false;
            } else if interactive {
                let link_uri = if in_link { self.link_uri(path) } else { None };
                if let Some(uri) = link_uri {
                    self.text(&name_for_url(&uri));
                } else if let Some(src) = o.attr("src") {
                    self.text(&name_for_url(src));
                }
            }
        } else if role == ROLE_PROGRESSBAR && has(raw, ATSPI_INDETERMINATE) {
            self.text(EMPTY_TEXT);
        } else if !name_is_content {
            let value = o
                .attr("valuetext")
                .map(str::to_owned)
                .or_else(|| o.value.map(format_value));
            if let Some(v) = value {
                self.text(&v);
            }
        }

        let length = self.units - start_units;
        let useful = length > 3 || self.useful_units > start_useful;
        if !is_editable
            && (name_is_content || role == ROLE_SECTION || role == ROLE_TEXTFRAME)
            && !useful
        {
            // fillVBuf puts this text before the field's other content, so that a link holding only
            // empty blocks is read on one line with its name.
            let at = self.out.len();
            if !name.is_empty() {
                self.text(&name);
            } else if role == ROLE_LINK
                && let Some(uri) = o.link_uri.as_deref()
            {
                self.text(&name_for_url(uri));
            }
            let written = self.out.split_off(at);
            self.out.splice(content_at..content_at, written);
        }
        if matches!(
            role,
            ROLE_TABLECELL | ROLE_TABLEROWHEADER | ROLE_TABLECOLUMNHEADER | ROLE_UNKNOWN
        ) && self.units == start_units
        {
            self.text(EMPTY_TEXT);
            self.out[flags_at] = b'0' as u16;
        }
        if (interactive || role == ROLE_SEPARATOR || !name.is_empty() || !o.description.is_empty())
            && self.units == start_units
        {
            self.text(EMPTY_TEXT);
        }
        self.out.push(b'E' as u16);

        let is_row_group =
            in_table && o.cell.is_none() && o.table.is_none() && role != ROLE_TABLEROW;
        // A field written flat holds none of what is below it, so a change there is written again
        // from the field that holds them all, and that field's children are never kept.
        self.marks.push(Mark {
            path: path.to_owned(),
            child_reuse: !child_ctx.no_reuse
                && !(in_table && o.cell.is_none())
                && child_ctx.table_id.is_none()
                && depth < MAX_DEPTH,
            escalate: is_row_group || depth > MAX_DEPTH,
            child_ctx,
        });
        if let Some(c) = selected_child
            && let Some(m) = self.marks.iter_mut().rev().find(|m| m.path == c)
        {
            m.escalate = true;
        }
        self.language = outer_language;
    }

    /// What an object says in a few words, for one that another names as its error message: its own
    /// text without embedded objects, or its name.
    fn summary(&self, path: &str) -> Option<String> {
        let o = self.objs.get(path)?;
        let text: String = o
            .text
            .as_deref()
            .unwrap_or("")
            .chars()
            .filter(|c| *c != EMBEDDED_OBJECT)
            .collect();
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        if !text.is_empty() {
            return Some(text);
        }
        Some(o.name.trim().to_owned()).filter(|n| !n.is_empty())
    }

    fn link_uri(&self, path: &str) -> Option<String> {
        let mut p = self.objs.get(path)?.parent.clone();
        for _ in 0..4 {
            let here = p?;
            let o = self.objs.get(&here)?;
            if ROLE_NAMES.get(o.role as usize).copied() == Some("link") {
                return o.link_uri.clone().filter(|u| !u.is_empty());
            }
            p = o.parent.clone();
        }
        None
    }

    /// The name of the item selected in a combo box's popup.
    fn combo_value(&self, o: &Obj) -> Option<String> {
        for c in &o.children {
            let Some(popup) = self.objs.get(c) else {
                continue;
            };
            for item in &popup.children {
                if let Some(i) = self.objs.get(item)
                    && has(i.states, ATSPI_SELECTED)
                {
                    return Some(i.name.clone());
                }
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_for_urls_follow_utils_cpp() {
        assert_eq!(name_for_url("file:///tmp/pages/second.html"), "second");
        assert_eq!(name_for_url("https://example.com/"), "example.com");
        assert_eq!(
            name_for_url("https://example.com/a/b/picture.png?x=1#top"),
            "picture x=1 top"
        );
        assert_eq!(
            name_for_url("mailto:someone@example.com"),
            "someone@example.com"
        );
        assert_eq!(name_for_url("data:image/png;base64,AAAA"), "");
        assert_eq!(name_for_url("picture.png"), "picture");
    }

    const TEXT: u32 = 1 << 13;
    const VISIBLE_ENABLED: [u32; 2] = [(1 << 30) | (1 << 8) | (1 << 24), 0];

    fn obj(role: u32, parent: Option<&str>, text: Option<&str>, children: &[&str]) -> Obj {
        Obj {
            parent: parent.map(str::to_owned),
            children: children.iter().map(|c| (*c).to_owned()).collect(),
            role,
            states: VISIBLE_ENABLED,
            interfaces: if text.is_some() { TEXT } else { 0 },
            text: text.map(str::to_owned),
            ..Obj::default()
        }
    }

    fn page() -> HashMap<String, Obj> {
        let mut objs = HashMap::new();
        objs.insert(
            "/a/1".into(),
            obj(95, None, Some("\u{fffc}\u{fffc}"), &["/a/2", "/a/4"]),
        );
        objs.insert(
            "/a/2".into(),
            obj(73, Some("/a/1"), Some("Hi \u{fffc}"), &["/a/3"]),
        );
        let mut link = obj(88, Some("/a/2"), Some("there"), &[]);
        link.name = "there".into();
        objs.insert("/a/3".into(), link);
        objs.insert("/a/4".into(), obj(56, Some("/a/1"), Some(""), &[]));
        objs
    }

    fn decoded(out: &[u16]) -> String {
        String::from_utf16(out).unwrap()
    }

    #[test]
    fn a_document_is_written_in_order_with_its_fields() {
        let objs = page();
        let none = HashSet::new();
        let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
        r.render("/a/1", Ctx::default());
        let s = decoded(&r.out);
        assert!(s.starts_with("C7,1,"), "{s}");
        let hi = s.find("T3:Hi 0;").expect("the paragraph's text");
        let link = s.find("C7,3,").expect("the link's field");
        let there = s.find("T5:there0;").expect("the link's text");
        assert!(hi < link && link < there, "{s}");
        // An empty cell reads as a space and is not a block of its own.
        assert!(s.contains("C7,4,0,"), "{s}");
        assert!(s.ends_with("T1: 0;EE"), "{s}");
        assert_eq!(r.nodes, 4);
        assert_eq!(r.marks.len(), 4);
    }

    #[test]
    fn an_update_replaces_a_field_and_keeps_the_children_it_is_told_to() {
        let mut objs = page();
        for o in objs.values_mut() {
            o.rendered = true;
        }
        let keep: HashSet<String> = ["/a/3".to_owned()].into();
        let mut r = Renderer::new(&objs, 7, "/a/1", false, &keep);
        r.replace("/a/2");
        let s = decoded(&r.out);
        assert!(s.starts_with("S7,2;C7,2,"), "{s}");
        assert!(s.contains("T3:Hi 0;R7,3;E"), "{s}");
        assert!(!s.contains("there"), "{s}");
        objs.get_mut("/a/3").unwrap().states = [0, 0];
        let none = HashSet::new();
        let mut r = Renderer::new(&objs, 7, "/a/1", true, &none);
        r.render("/a/1", Ctx::default());
        // The top of a document still being walked stops at the first object not complete.
        assert_eq!(decoded(&r.out), "");
    }

    #[test]
    fn a_link_with_nothing_in_it_is_named_before_its_empty_blocks() {
        let mut objs = HashMap::new();
        objs.insert("/a/1".into(), obj(95, None, Some("\u{fffc}"), &["/a/2"]));
        let mut link = obj(
            88,
            Some("/a/1"),
            Some("\u{fffc}\u{fffc}"),
            &["/a/3", "/a/4"],
        );
        link.link_uri = Some("https://example.com/explore".into());
        objs.insert("/a/2".into(), link);
        let mut block = obj(85, Some("/a/2"), Some(""), &[]);
        block.attributes.push(("display".into(), "block".into()));
        objs.insert("/a/3".into(), block.clone());
        objs.insert("/a/4".into(), block);
        let none = HashSet::new();
        let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
        r.render("/a/1", Ctx::default());
        let s = decoded(&r.out);
        let name = s
            .find("T7:explore")
            .expect("the link's name from its address");
        let first_block = s.find("C7,3,").expect("the first empty block");
        assert!(name < first_block, "{s}");
    }

    #[test]
    fn text_carries_the_language_of_its_object_or_its_ancestor() {
        let mut objs = page();
        objs.get_mut("/a/1").unwrap().locale = Some("pl".into());
        objs.get_mut("/a/3").unwrap().locale = Some("en".into());
        let none = HashSet::new();
        let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
        r.render("/a/1", Ctx::default());
        let s = decoded(&r.out);
        assert!(s.contains("T3:Hi 1;8:language2:pl"), "{s}");
        assert!(s.contains("T5:there1;8:language2:en"), "{s}");
        assert!(s.ends_with("T1: 1;8:language2:plEE"), "{s}");
        let mut objs = page();
        objs.get_mut("/a/1").unwrap().locale = Some("pl".into());
        for o in objs.values_mut() {
            o.rendered = true;
        }
        let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
        r.replace("/a/3");
        let s = decoded(&r.out);
        // A replaced object's text has its ancestors' language too.
        assert!(s.contains("T5:there1;8:language2:pl"), "{s}");
    }

    #[test]
    fn a_field_says_it_is_clickable_has_details_and_its_error_message() {
        let mut objs = page();
        let p = objs.get_mut("/a/2").unwrap();
        p.action = Some("click".into());
        p.has_details = true;
        p.error_targets = vec!["/a/3".into()];
        let none = HashSet::new();
        let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
        r.render("/a/1", Ctx::default());
        let s = decoded(&r.out);
        assert!(s.contains("19:atspi::action_click1:1"), "{s}");
        assert!(s.contains("10:hasDetails4:true"), "{s}");
        assert!(s.contains("12:errorMessage5:there"), "{s}");
    }

    /// The identifiers of the fields in a stream, the text in order, and how deep fields nest.
    fn shape(out: &[u16]) -> (Vec<i64>, String, usize) {
        let mut at = 0;
        let number = |at: &mut usize| {
            let start = *at;
            while out[*at] != b',' as u16 && out[*at] != b';' as u16 && out[*at] != b':' as u16 {
                *at += 1;
            }
            let n: i64 = String::from_utf16(&out[start..*at])
                .unwrap()
                .parse()
                .unwrap();
            *at += 1;
            n
        };
        let string = |at: &mut usize| {
            let n = number(at) as usize;
            *at += n;
            String::from_utf16(&out[*at - n..*at]).unwrap()
        };
        let (mut ids, mut text, mut depth, mut deepest) = (Vec::new(), String::new(), 0, 0);
        while at < out.len() {
            let op = out[at];
            at += 1;
            if op == b'C' as u16 || op == b'T' as u16 {
                if op == b'C' as u16 {
                    let _handle: i64 = number(&mut at);
                    ids.push(number(&mut at));
                    let _flags: i64 = number(&mut at);
                    depth += 1;
                    deepest = deepest.max(depth);
                } else {
                    text += &string(&mut at);
                }
                let pairs = number(&mut at);
                for _ in 0..pairs * 2 {
                    string(&mut at);
                }
            } else if op == b'E' as u16 {
                depth -= 1;
            } else {
                assert!(op == b'S' as u16 || op == b'R' as u16, "op {op}");
                number(&mut at);
                ids.push(-number(&mut at));
            }
        }
        assert_eq!(depth, 0, "every field is closed");
        (ids, text, deepest)
    }

    /// A document of sections nested n deep, each saying its level, with the paths in order.
    fn nested(n: usize) -> (HashMap<String, Obj>, Vec<String>) {
        let paths: Vec<String> = (1..=n).map(|i| format!("/a/{i}")).collect();
        let mut objs = HashMap::new();
        for (i, path) in paths.iter().enumerate() {
            let parent = i.checked_sub(1).map(|p| paths[p].as_str());
            let text = format!("{i} \u{fffc}");
            let children: Vec<&str> = paths.get(i + 1).map(String::as_str).into_iter().collect();
            objs.insert(path.clone(), obj(85, parent, Some(&text), &children));
        }
        (objs, paths)
    }

    /// Deeper than the engine's stack could recurse, in a release build or a debug one.
    const DEEP: usize = 50_000;

    /// Runs f on a thread with the engine thread's stack, where the engine renders.
    fn on_engine_stack<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
        std::thread::Builder::new()
            .stack_size(crate::ENGINE_STACK)
            .spawn(f)
            .unwrap()
            .join()
            .unwrap()
    }

    #[test]
    fn a_document_nested_past_the_limit_is_written_flat_below_it_in_order() {
        let n = DEEP;
        let (ids, text, deepest, marks) = on_engine_stack(move || {
            let (objs, _) = nested(n);
            let none = HashSet::new();
            let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
            r.render("/a/1", Ctx::default());
            let (ids, text, deepest) = shape(&r.out);
            let marks: HashMap<String, (bool, bool)> = r
                .marks
                .into_iter()
                .map(|m| (m.path, (m.child_reuse, m.escalate)))
                .collect();
            (ids, text, deepest, marks)
        });
        // The document's field counts as one deep.
        assert_eq!(deepest, MAX_DEPTH + 2);
        assert_eq!(ids, (1..=n as i64).collect::<Vec<_>>());
        let expected: String = (0..n).map(|i| format!("{i} ")).collect();
        assert_eq!(text, expected);
        let mark = |i: usize| marks[&format!("/a/{i}")];
        // A change below the field at the limit, /a/{MAX_DEPTH + 1}, writes that field again
        // whole, and none of the fields in it are kept.
        assert_eq!(mark(MAX_DEPTH), (true, false));
        assert_eq!(mark(MAX_DEPTH + 1), (false, false));
        assert_eq!(mark(MAX_DEPTH + 2), (false, true));
        assert_eq!(mark(n), (false, true));
    }

    #[test]
    fn an_update_deep_in_a_document_is_written_flat_below_the_limit() {
        let n = DEEP;
        let (ids, deepest) = on_engine_stack(move || {
            let (mut objs, paths) = nested(n);
            for o in objs.values_mut() {
                o.rendered = true;
            }
            let none = HashSet::new();
            let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
            r.replace(&paths[MAX_DEPTH - 2]);
            let (ids, _, deepest) = shape(&r.out);
            (ids, deepest)
        });
        assert_eq!(ids[0], -(MAX_DEPTH as i64 - 1));
        assert_eq!(
            ids[1..],
            (MAX_DEPTH as i64 - 1..=n as i64).collect::<Vec<_>>()
        );
        // The replaced field is MAX_DEPTH - 2 deep already.
        assert_eq!(deepest, 4);
    }

    #[test]
    fn a_static_inline_element_with_explicit_name_still_renders_its_children_and_text() {
        let mut objs = HashMap::new();
        objs.insert(
            "/a/1".into(),
            obj(95, None, Some("\u{fffc}\u{fffc}\u{fffc}"), &["/a/2", "/a/4", "/a/5"]),
        );
        // <span title="span title"><a href="...">Info</a></span> in Gecko: role 116 ("static").
        let mut span_link = obj(116, Some("/a/1"), Some("\u{fffc}"), &["/a/3"]);
        span_link.name = "span title".into();
        span_link.attributes.push(("tag".into(), "span".into()));
        span_link.attributes.push(("display".into(), "inline".into()));
        span_link.attributes.push(("explicit-name".into(), "true".into()));
        objs.insert("/a/2".into(), span_link);

        let mut link = obj(88, Some("/a/2"), Some("Info"), &[]);
        link.name = "Info".into();
        link.attributes.push(("tag".into(), "a".into()));
        objs.insert("/a/3".into(), link);

        // <span title="plain title">Plain text</span>
        let mut span_text = obj(116, Some("/a/1"), Some("Plain text"), &[]);
        span_text.name = "plain title".into();
        span_text.attributes.push(("tag".into(), "span".into()));
        span_text.attributes.push(("explicit-name".into(), "true".into()));
        objs.insert("/a/4".into(), span_text);

        // <span title="empty title"></span> falls back to its explicit name.
        let mut span_empty = obj(116, Some("/a/1"), Some(""), &[]);
        span_empty.name = "empty title".into();
        span_empty.attributes.push(("tag".into(), "span".into()));
        span_empty.attributes.push(("explicit-name".into(), "true".into()));
        objs.insert("/a/5".into(), span_empty);

        let none = HashSet::new();
        let mut r = Renderer::new(&objs, 7, "/a/1", false, &none);
        r.render("/a/1", Ctx::default());
        let s = decoded(&r.out);
        assert!(s.contains("C7,3,"), "link child must be rendered: {s}");
        assert!(s.contains("T4:Info0;"), "link text must be rendered: {s}");
        assert!(!s.contains("T10:span title0;"), "span title must not replace child link: {s}");
        assert!(s.contains("T10:Plain text0;"), "span text must be rendered: {s}");
        assert!(s.contains("T11:empty title0;"), "empty span must fall back to name: {s}");
    }

    #[test]
    fn ids_are_the_path_number_as_signed() {
        assert_eq!(id_of("/org/a11y/atspi/accessible/42"), Some(42));
        assert_eq!(
            id_of("/org/a11y/atspi/accessible/2147483648"),
            Some(i32::MIN)
        );
        assert_eq!(id_of("/org/a11y/atspi/accessible/root"), None);
        let webkit = "/org/a11y/atspi/accessible/c557039c_e1fa_427b_88ec_9655898d108f";
        let id = id_of(webkit).unwrap();
        assert_eq!(id_of(webkit), Some(id));
        assert_eq!(crate::named::path(id).as_deref(), Some(webkit));
    }
}
