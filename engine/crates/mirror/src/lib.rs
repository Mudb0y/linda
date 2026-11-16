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
pub mod time;
pub mod tree;

use client::CallFailure;

#[derive(Debug)]
pub enum Error {
    Dbus(zbus::Error),
    /// A call to an application failed: which application, object and
    /// method, and how.
    Call(Box<CallFailure>),
    NotFound(String),
    /// Connecting, or the handshake after it, did not finish in time.
    TimedOut(String),
    /// An address the engine will not connect to.
    Refused(String),
    Io(std::io::Error),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Dbus(e) => write!(f, "D-Bus: {e}"),
            Error::Call(c) => write!(f, "{c}"),
            Error::NotFound(s) => f.write_str(s),
            Error::TimedOut(s) => write!(f, "{s}: timed out"),
            Error::Refused(s) => write!(f, "refused: {s}"),
            Error::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<zbus::Error> for Error {
    fn from(e: zbus::Error) -> Self {
        Error::Dbus(e)
    }
}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

impl From<CallFailure> for Error {
    fn from(c: CallFailure) -> Self {
        Error::Call(Box::new(c))
    }
}
