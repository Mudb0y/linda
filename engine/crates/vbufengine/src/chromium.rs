//! What Chromium, and every Electron program with it, exposes differently from Firefox.
//!
//! Chromium gives each run of text an accessible of its own, a static text leaf, as a child of the
//! element whose text holds it; that element's text then holds the run's characters themselves, and
//! an embedded object character only for each of its other children. Firefox exposes no text leaves,
//! so there the n-th embedded object character is simply the n-th child.

use std::collections::HashMap;

use crate::tables::ROLE_NAMES;
use crate::walk::Obj;

/// Whether a document attribute holds the document's address: Firefox's DocURL, Chromium's URI.
pub fn is_doc_url(key: &str) -> bool {
    key == "DocURL" || key == "URI"
}

/// The children that an object's embedded object characters stand for, in order, given how many of
/// those characters its text has.
pub fn embedded_children<'a>(
    o: &'a Obj,
    objs: &HashMap<String, Obj>,
    embedded: usize,
) -> Vec<&'a String> {
    if o.children.len() <= embedded {
        return o.children.iter().collect();
    }
    o.children
        .iter()
        .filter(|c| !objs.get(*c).is_some_and(is_text_leaf))
        .collect()
}

/// A run of text, a line break or a list marker, whose characters are in its parent's text, as
/// Chromium's static objects without children are. Firefox's are images whose picture did not load,
/// which stand for an embedded object character.
fn is_text_leaf(o: &Obj) -> bool {
    ROLE_NAMES.get(o.role as usize).copied() == Some("static")
        && o.children.is_empty()
        && o.attr("tag") != Some("img")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obj(role: &str, tag: Option<&str>, children: &[&str]) -> Obj {
        let role = ROLE_NAMES.iter().position(|r| *r == role).unwrap() as u32;
        Obj {
            role,
            children: children.iter().map(|c| c.to_string()).collect(),
            attributes: tag
                .map(|t| vec![("tag".to_owned(), t.to_owned())])
                .unwrap_or_default(),
            ..Default::default()
        }
    }

    #[test]
    fn text_leaves_are_skipped_only_when_children_outnumber_embedded_characters() {
        let mut objs = HashMap::new();
        objs.insert("/a/2".to_owned(), obj("static", None, &[]));
        objs.insert("/a/3".to_owned(), obj("link", Some("a"), &["/a/4"]));
        objs.insert("/a/4".to_owned(), obj("static", None, &[]));
        objs.insert("/a/5".to_owned(), obj("static", None, &[]));
        objs.insert("/a/6".to_owned(), obj("static", Some("img"), &[]));
        objs.insert("/a/7".to_owned(), obj("static", Some("::marker"), &[]));
        // "Read <link> or <image>.", as Chromium gives it.
        let paragraph = obj("paragraph", Some("p"), &["/a/2", "/a/3", "/a/5", "/a/6"]);
        assert_eq!(
            embedded_children(&paragraph, &objs, 2),
            vec!["/a/3", "/a/6"]
        );
        // A list item's marker and link.
        let item = obj("list item", Some("li"), &["/a/7", "/a/3"]);
        assert_eq!(embedded_children(&item, &objs, 1), vec!["/a/3"]);
        // As Firefox gives it, with no text leaves.
        let paragraph = obj("paragraph", Some("p"), &["/a/3", "/a/6"]);
        assert_eq!(
            embedded_children(&paragraph, &objs, 2),
            vec!["/a/3", "/a/6"]
        );
    }
}
