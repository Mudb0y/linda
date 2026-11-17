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

    /// Writes path's field, and everything in it.
    pub fn render(&mut self, path: &str, ctx: Ctx) {
        self.language = self.language_for(path);
        self.node(path, ctx, true);
    }

    /// Writes an S, then path's field.
    pub fn replace(&mut self, path: &str) {
        let Some(id) = id_of(path) else { return };
        self.out.push(b'S' as u16);
        push(&mut self.out, &format!("{},{};", self.doc_handle, id));
        let ctx = self.ctx_for(path);
        self.language = self.language_for(path);
        self.node(path, ctx, true);
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

    fn node(&mut self, path: &str, ctx: Ctx, is_start: bool) {
        if self.stopped {
            return;
        }
        if !is_start && self.reuse.contains(path) {
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
                self.node(c, ctx, false);
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
                    self.node(child, child_ctx, false);
                }
                index += 1;
            }
            self.text(&chunk);
        } else if render_children && text.is_empty() && !o.children.is_empty() {
            for child in &o.children {
                self.node(child, child_ctx, false);
            }
        } else if render_selected_only {
            let selected = o
                .children
                .iter()
                .find(|c| objs.get(*c).is_some_and(|k| has(k.states, ATSPI_SELECTED)))
                .or(o.children.first());
            if let Some(c) = selected {
                selected_child = Some(c.clone());
                self.node(c, child_ctx, false);
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
        self.marks.push(Mark {
            path: path.to_owned(),
            child_reuse: !child_ctx.no_reuse
                && !(in_table && o.cell.is_none())
                && child_ctx.table_id.is_none(),
            escalate: is_row_group,
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
