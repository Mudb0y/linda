//! A client-side mirror of an application's AT-SPI tree: finding the
//! accessibility bus, activating an application's bridge, fetching its tree
//! by several methods, decoding it into a plain [`tree::Tree`], and keeping
//! that tree current from signals.

pub mod app;
pub mod client;
pub mod consts;
pub mod decode;
pub mod discovery;
pub mod fetch;
pub mod maintain;
pub mod tree;

#[derive(Debug)]
pub enum Error {
    Dbus(zbus::Error),
    NotFound(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Dbus(e) => write!(f, "D-Bus: {e}"),
            Error::NotFound(s) => f.write_str(s),
        }
    }
}

impl std::error::Error for Error {}

impl From<zbus::Error> for Error {
    fn from(e: zbus::Error) -> Self {
        Error::Dbus(e)
    }
}
