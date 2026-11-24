//! The part of libpwsink that needs no PipeWire: the sample formats and gain stage, each stream's
//! ring, position and clock, and the list of output devices. libpwsink drives these from its
//! threads; the tests here drive them by hand, standing in for the real-time callback and the loop
//! thread.

pub mod devices;
pub mod format;
pub mod stream;
