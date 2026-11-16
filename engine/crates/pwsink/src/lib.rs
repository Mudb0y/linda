//! Plays NVDA's audio through PipeWire, with a C interface for source/linuxPlatform/audioOutput/
//! pipewireSink.py to load through ctypes.
//!
//! Each of NVDA's players gets a stream: a lock-free ring its producer fills and a real-time
//! callback on PipeWire's data thread drains, applying channel and process gains as it copies.
//! No Python runs on a PipeWire thread, and no call made from Python waits for PipeWire, its
//! daemon or the thread that runs the connection, so stopping speech is one atomic store that
//! the callback obeys from its next cycle.
//!
//! The library keeps time by the wall clock whenever the graph stops consuming a stream that holds
//! audio, and it reconnects by itself when the daemon goes and comes back. A player in front of
//! it therefore always finishes its feeds, its syncs and its callbacks in real time, heard or not,
//! and the frames delivered say which.
//!
//! Every function taking a stream handle must be given a handle from pwsink_open that has not yet
//! been passed to pwsink_close, and must not be called on one handle from two threads at once.

mod engine;

use std::ffi::{CStr, CString, c_char, c_int};
use std::sync::Arc;
use std::sync::atomic::Ordering::Relaxed;

use engine::{DELIVERED, PROCESS_GAIN, engine};
use pwsink_core::format::Format;
use pwsink_core::stream::{NS, Stream};

/// Nanoseconds on CLOCK_MONOTONIC, the clock PipeWire's graph times are on.
pub fn mono_ns() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock_gettime fills the local.
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as u64 * NS + ts.tv_nsec as u64
}

/// Starts the connection's thread, which connects in the background. Returns 0, or a negative
/// errno when the thread could not be made.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_init() -> c_int {
    match engine() {
        Ok(_) => 0,
        Err(e) => e,
    }
}

/// Opens a stream for frames of the WAVEFORMATEX fields given, played to the node named target,
/// or to the default sink when target is null or empty or names no node. Returns a handle at
/// once, whether or not PipeWire is there; null with *err set to a negative errno only for a
/// format PipeWire is not given or when the library could not start.
///
/// # Safety
/// target is null or a NUL-terminated string; err is null or writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_open(
    format_tag: u32,
    channels: u32,
    rate: u32,
    bits: u32,
    target: *const c_char,
    err: *mut c_int,
) -> *const Stream {
    let fail = |e: c_int| {
        if !err.is_null() {
            // SAFETY: err is writable when not null.
            unsafe { *err = e };
        }
        std::ptr::null()
    };
    let Some(format) = Format::from_wave(format_tag, channels, rate, bits) else {
        return fail(-libc::EINVAL);
    };
    let target = if target.is_null() {
        None
    } else {
        // SAFETY: target is a NUL-terminated string when not null.
        let name = unsafe { CStr::from_ptr(target) };
        (!name.is_empty()).then(|| CString::from(name))
    };
    let engine = match engine() {
        Ok(engine) => engine,
        Err(e) => return fail(e),
    };
    let now = mono_ns();
    if let Some(stream) = engine.take_pooled(&format, &target) {
        stream.reuse(now);
        return Arc::into_raw(stream);
    }
    // A stream opened before the library has first connected waits for its pw_stream as one opened
    // while connected does, rather than keeping time as if there were no daemon.
    let stream = Arc::new(Stream::new(format, target, now, !engine.absent()));
    engine.register(stream.clone());
    Arc::into_raw(stream)
}

/// Copies the whole frames in data into the stream's ring without blocking, and returns how many
/// it took: all of them, or none when they do not all fit.
///
/// # Safety
/// A live handle, and bytes readable at data.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_write(stream: *const Stream, data: *const u8, bytes: usize) -> u64 {
    // SAFETY: as documented.
    let (s, data) = unsafe { (&*stream, std::slice::from_raw_parts(data, bytes)) };
    let (frames, kick) = s.write(data, mono_ns());
    if kick {
        wake();
    }
    frames
}

/// Frames written and neither taken by the graph nor discarded.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_buffered(stream: *const Stream) -> u64 {
    // SAFETY: as documented.
    unsafe { &*stream }.buffered(mono_ns())
}

/// Frames heard, or discarded by the clock, since the stream was opened or last reset.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_played(stream: *const Stream) -> u64 {
    // SAFETY: as documented.
    unsafe { &*stream }.played(mono_ns())
}

/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_start(stream: *const Stream) {
    // SAFETY: as documented.
    if unsafe { &*stream }.start(mono_ns()) {
        wake();
    }
}

/// Silences the stream from the graph's next cycle. One atomic store.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_stop(stream: *const Stream) {
    // SAFETY: as documented.
    unsafe { &*stream }.stop();
}

/// Discards what is queued and counts from zero again.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_reset(stream: *const Stream) {
    // SAFETY: as documented.
    unsafe { &*stream }.reset(mono_ns());
}

/// Sets one channel's gain, from 0 to 1. Returns 0, or -EINVAL for a channel the stream lacks or
/// a level out of range.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_set_volume(
    stream: *const Stream,
    channel: u32,
    level: f32,
) -> c_int {
    // SAFETY: as documented.
    if unsafe { &*stream }.set_volume(channel, level) {
        0
    } else {
        -libc::EINVAL
    }
}

/// Stops and resets the stream and keeps it in the pool for the next stream of its format and
/// target. The handle is not used again.
///
/// # Safety
/// A live handle, which this consumes.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_close(stream: *const Stream) {
    if stream.is_null() {
        return;
    }
    // SAFETY: the handle came from Arc::into_raw in pwsink_open.
    let s = unsafe { Arc::from_raw(stream) };
    let now = mono_ns();
    s.stop();
    s.reset(now);
    match engine() {
        Ok(engine) => engine.pool_put(s, now),
        Err(_) => drop(s),
    }
}

/// Sets the gain applied to every stream after its channel gains, from 0 to 1. Returns 0, or
/// -EINVAL for a level out of range.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_set_gain(level: f32) -> c_int {
    if !(0.0..=1.0).contains(&level) {
        return -libc::EINVAL;
    }
    PROCESS_GAIN.store(level.to_bits(), Relaxed);
    0
}

/// Frames the graph has taken from every stream since the library was loaded, not counting frames
/// discarded by the clock.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_delivered() -> u64 {
    DELIVERED.load(Relaxed)
}

/// Keeps every stream active for ms milliseconds from now, so that the device it plays to does
/// not suspend; after that, streams with nothing to play are deactivated until they have.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_keep_awake(ms: u64) {
    if let Ok(engine) = engine() {
        engine.keep_awake(ms, mono_ns());
    }
}

fn wake() {
    if let Ok(engine) = engine() {
        engine.signal();
    }
}

// What the library is doing, for tests and diagnostics.

/// 1 while connected to a PipeWire daemon.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_connected() -> c_int {
    engine().is_ok_and(|e| e.connected()) as c_int
}

/// Process calls the stream has had since it was last given a pw_stream.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_process_calls(stream: *const Stream) -> u64 {
    // SAFETY: as documented.
    unsafe { &*stream }.process_calls()
}

/// Frames of the stream PipeWire's resampler held in its last cycle. They play after anything
/// handed over, a stop included.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_resampler_frames(stream: *const Stream) -> u64 {
    // SAFETY: as documented.
    unsafe { &*stream }.resampler_frames()
}

/// 1 when the stream has a pw_stream and it is active.
///
/// # Safety
/// A live handle.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_is_active(stream: *const Stream) -> c_int {
    // SAFETY: as documented.
    let s = unsafe { &*stream };
    (s.is_attached() && s.is_active()) as c_int
}

/// How many closed streams the pool holds.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_pool_size() -> u64 {
    engine().map_or(0, |e| e.pool_size() as u64)
}

/// How many streams have a pw_stream.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_attached_streams() -> u64 {
    engine().map_or(0, |e| e.attached_count() as u64)
}

/// Changes how many closed streams the pool keeps, 8 by default, and how long an unused one is
/// kept, 60 seconds by default.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_set_pool_limits(max: u64, idle_ms: u64) {
    if let Ok(engine) = engine() {
        engine.set_pool_limits(max as usize, idle_ms);
    }
}

/// Occupies the connection's thread for ms milliseconds, inside the loop's lock, so that a test can
/// show that nothing called from Python waits for that thread or its lock. It does nothing unless
/// BASEDREADER_PWSINK_TEST_HOOKS=1 was in the environment when the library started: anything else
/// that called it would hold up connecting, attaching and waking every stream.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_block_loop(ms: u64) {
    if let Ok(engine) = engine() {
        engine.block_loop(ms);
    }
}
