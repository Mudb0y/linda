//! Names and numbers from at-spi2-core 2.62 (`xml/*.xml`, `atspi/atspi-constants.h`).

pub const ROOT_PATH: &str = "/org/a11y/atspi/accessible/root";
pub const NULL_PATH: &str = "/org/a11y/atspi/null";
pub const CACHE_PATH: &str = "/org/a11y/atspi/cache";
pub const REGISTRY_PATH: &str = "/org/a11y/atspi/registry";
pub const REGISTRY_NAME: &str = "org.a11y.atspi.Registry";

pub const IFACE_ACCESSIBLE: &str = "org.a11y.atspi.Accessible";
pub const IFACE_APPLICATION: &str = "org.a11y.atspi.Application";
pub const IFACE_CACHE: &str = "org.a11y.atspi.Cache";
pub const IFACE_COLLECTION: &str = "org.a11y.atspi.Collection";
pub const IFACE_TEXT: &str = "org.a11y.atspi.Text";
pub const IFACE_PROPERTIES: &str = "org.freedesktop.DBus.Properties";
pub const EVENT_PREFIX: &str = "org.a11y.atspi.Event.";

pub const ROLE_DOCUMENT_WEB: u32 = 95;
pub const ROLE_HEADING: u32 = 83;

pub const STATE_BUSY: u32 = 3;
pub const STATE_DEFUNCT: u32 = 6;
pub const STATE_MANAGES_DESCENDANTS: u32 = 31;

/// `AtspiStateType` in enum order, spelled as the ATK bridge spells the minor
/// part of `object:state-changed:<name>`.
const STATE_NAMES: [&str; 44] = [
    "invalid",
    "active",
    "armed",
    "busy",
    "checked",
    "collapsed",
    "defunct",
    "editable",
    "enabled",
    "expandable",
    "expanded",
    "focusable",
    "focused",
    "has-tooltip",
    "horizontal",
    "iconified",
    "modal",
    "multi-line",
    "multiselectable",
    "opaque",
    "pressed",
    "resizable",
    "selectable",
    "selected",
    "sensitive",
    "showing",
    "single-line",
    "stale",
    "transient",
    "vertical",
    "visible",
    "manages-descendants",
    "indeterminate",
    "required",
    "truncated",
    "animated",
    "invalid-entry",
    "supports-autocompletion",
    "selectable-text",
    "is-default",
    "visited",
    "checkable",
    "has-popup",
    "read-only",
];

pub fn state_from_name(name: &str) -> Option<u32> {
    // ATK names this one "default"; AT-SPI's enum calls it IS_DEFAULT.
    let name = if name == "default" {
        "is-default"
    } else {
        name
    };
    STATE_NAMES
        .iter()
        .position(|n| *n == name)
        .map(|i| i as u32)
}

pub fn state_name(bit: u32) -> &'static str {
    STATE_NAMES.get(bit as usize).copied().unwrap_or("unknown")
}

pub fn has_state(states: [u32; 2], bit: u32) -> bool {
    bit < 64 && states[(bit / 32) as usize] & (1 << (bit % 32)) != 0
}

pub fn set_state(states: &mut [u32; 2], bit: u32, on: bool) {
    if bit >= 64 {
        return;
    }
    let word = &mut states[(bit / 32) as usize];
    if on {
        *word |= 1 << (bit % 32);
    } else {
        *word &= !(1 << (bit % 32));
    }
}

/// The interfaces a client distinguishes, as bits, so a node does not carry a
/// list of long strings.
const INTERFACE_NAMES: [&str; 18] = [
    "org.a11y.atspi.Accessible",
    "org.a11y.atspi.Action",
    "org.a11y.atspi.Application",
    "org.a11y.atspi.Collection",
    "org.a11y.atspi.Component",
    "org.a11y.atspi.Document",
    "org.a11y.atspi.EditableText",
    "org.a11y.atspi.Hyperlink",
    "org.a11y.atspi.Hypertext",
    "org.a11y.atspi.Image",
    "org.a11y.atspi.Selection",
    "org.a11y.atspi.Table",
    "org.a11y.atspi.TableCell",
    "org.a11y.atspi.Text",
    "org.a11y.atspi.Value",
    "org.a11y.atspi.Socket",
    "org.a11y.atspi.StreamableContent",
    "org.a11y.atspi.Cache",
];

pub const INTERFACE_TEXT_BIT: u32 = 1 << 13;

pub fn interface_bits<'a>(names: impl IntoIterator<Item = &'a str>) -> u32 {
    names.into_iter().fold(0, |acc, n| {
        acc | INTERFACE_NAMES
            .iter()
            .position(|k| *k == n)
            .map_or(0, |i| 1 << i)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_names_match_the_enum() {
        assert_eq!(state_from_name("busy"), Some(STATE_BUSY));
        assert_eq!(state_from_name("defunct"), Some(STATE_DEFUNCT));
        assert_eq!(
            state_from_name("manages-descendants"),
            Some(STATE_MANAGES_DESCENDANTS)
        );
        assert_eq!(state_from_name("read-only"), Some(43));
        assert_eq!(state_from_name("default"), Some(39));
        assert_eq!(state_from_name("no-such-state"), None);
    }

    #[test]
    fn state_bits_round_trip() {
        let mut s = [0u32; 2];
        set_state(&mut s, 12, true);
        set_state(&mut s, 43, true);
        assert_eq!(s, [1 << 12, 1 << 11]);
        assert!(has_state(s, 43));
        set_state(&mut s, 12, false);
        assert!(!has_state(s, 12));
    }

    #[test]
    fn interface_bits_ignore_unknown_names() {
        let bits = interface_bits(["org.a11y.atspi.Text", "org.example.Other"]);
        assert_eq!(bits, INTERFACE_TEXT_BIT);
    }
}
