//! Identifiers for objects whose path does not end in a number. The ATK bridge and Qt number their
//! objects, and the buffer takes that number as the object's ID; WebKitGTK names each object with a
//! UUID, so its documents had no IDs and rendered empty. Such a path is given a number of its own
//! the first time it is seen, kept for the life of the process so that a re-render and NVDA's side
//! agree on it, counting down from u32::MAX - 1: the ATK bridge counts up from 1 and Qt up from
//! 2^31, so neither reaches these.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};

const FIRST: u32 = u32::MAX - 1;

#[derive(Default)]
struct Named {
    ids: HashMap<String, i32>,
    paths: HashMap<i32, String>,
}

static NAMED: LazyLock<Mutex<Named>> = LazyLock::new(Mutex::default);

/// The ID of a path whose last part is not a number, given one if it has none yet.
pub fn id(path: &str) -> i32 {
    let mut n = NAMED.lock().unwrap();
    if let Some(&id) = n.ids.get(path) {
        return id;
    }
    let id = FIRST.wrapping_sub(n.ids.len() as u32) as i32;
    n.ids.insert(path.to_owned(), id);
    n.paths.insert(id, path.to_owned());
    id
}

/// The path given the ID, if one was.
pub fn path(id: i32) -> Option<String> {
    NAMED.lock().unwrap().paths.get(&id).cloned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_named_path_keeps_its_id_and_leads_back_to_it() {
        let a = "/org/a11y/atspi/accessible/9aeab73a_d0d4_4d11_a74f_6b13de39d0d7";
        let b = "/org/a11y/atspi/accessible/27ab826a_05ab_456d_b838_b8736bc66fc4";
        let (ia, ib) = (id(a), id(b));
        assert_ne!(ia, ib);
        assert_eq!(id(a), ia);
        assert!((ia as u32) > 1 << 31 && (ib as u32) > 1 << 31);
        assert_eq!(path(ia).as_deref(), Some(a));
        assert_eq!(path(ib).as_deref(), Some(b));
        assert_eq!(path(5), None);
    }
}
