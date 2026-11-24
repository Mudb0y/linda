//! Plays NVDA's audio through PipeWire, with a C interface for source/linuxPlatform/audioOutput/
//! pipewireSink.py to load through ctypes.
//!
//! Each of NVDA's players gets a stream: a lock-free ring its producer fills and a real-time
//! callback on PipeWire's data thread drains, applying channel and process gains as it copies.
//! No Python runs on a PipeWire thread, and no call made from Python waits for PipeWire, its
//! daemon or the thread that runs the connection, so stopping speech is one atomic store that
//! the callback obeys from its next cycle. The one exception is pwsink_devices, which is never on
//! the way to playing a sound, and waits a bounded time only until the first roundtrip after the
//! library connects has been answered.
//!
//! The library keeps time by the wall clock whenever the graph stops consuming a stream that holds
//! audio, and it reconnects by itself when the daemon goes and comes back. A player in front of
//! it therefore always finishes its feeds, its syncs and its callbacks in real time, heard or not,
//! and the frames delivered say which.
//!
//! Every function taking a stream handle must be given a handle from pwsink_open that has not yet
//! been passed to pwsink_close. No two of them may run at once on one handle, but for pwsink_stop
//! and pwsink_abort: those two may run on any thread at the same time as any other function on the
//! same handle, each other included, though never at the same time as pwsink_close. Each is one
//! atomic store, with no lock, no wake and no allocation, so it never blocks and can be called
//! without letting go of Python's GIL.
//!
//! The library also lists the sinks there are to play to, for NVDA's choice of output device. A
//! stream opened with no target follows the default sink, which WirePlumber moves it to whenever
//! the default changes. One opened with a target that is absent, or that goes away while it plays,
//! is linked to the default sink by WirePlumber, and moved back once the target is there again:
//! the library sets nothing, such as node.dont-fallback or node.dont-reconnect, that would leave
//! the stream unlinked instead. That is WirePlumber's policy, and so is one more rule of it: a
//! stream linked to its target while the target is the default sink follows the default from then
//! on, as PulseAudio's streams do, unless linking.follow-default-target is turned off.

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

/// Silences the stream from the graph's next cycle until pwsink_start. One atomic store.
///
/// # Safety
/// A live handle; on any thread, but not alongside pwsink_close.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_stop(stream: *const Stream) {
    // SAFETY: as documented.
    unsafe { &*stream }.stop();
}

/// Silences the stream from the graph's next cycle until pwsink_reset, whatever pwsink_start is
/// called meanwhile. One atomic store.
///
/// # Safety
/// A live handle; on any thread, but not alongside pwsink_close.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_abort(stream: *const Stream) {
    // SAFETY: as documented.
    unsafe { &*stream }.abort();
}

/// Discards what is queued, counts from zero again and ends an abort.
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

/// One sink, as pwsink_devices lists it. Both strings are UTF-8 and NUL-terminated.
#[repr(C)]
pub struct PwsinkDevice {
    /// The sink's node.name, which pwsink_open takes as its target.
    pub name: *mut c_char,
    /// What to show for it: its node.description, or its node.name when it has none.
    pub description: *mut c_char,
}

/// The sinks present, in the order PipeWire announced them, as an array of *count devices, or
/// null with *count 0 when there are none. The array and its strings belong to the library: they
/// stay valid, and unchanged, until the array is passed to pwsink_free_devices, which must be done
/// once for each array that is not null, and must not be done with any other pointer.
///
/// The library learns of the sinks from PipeWire's registry once it has connected. Until the first
/// roundtrip on a connection completes, which on a local daemon takes milliseconds, the list may
/// lack some, so this waits for it for up to wait_ms milliseconds. It does not wait while the
/// daemon is known to be away, when there are no sinks to list.
///
/// # Safety
/// count is writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_devices(wait_ms: u64, count: *mut usize) -> *mut PwsinkDevice {
    let devices = match engine() {
        Ok(engine) => engine.devices(wait_ms.saturating_mul(1_000_000)),
        Err(_) => Vec::new(),
    };
    // SAFETY: count is writable.
    unsafe { *count = devices.len() };
    if devices.is_empty() {
        return std::ptr::null_mut();
    }
    let listed: Box<[PwsinkDevice]> = devices
        .into_iter()
        .map(|d| PwsinkDevice {
            name: d.name.into_raw(),
            description: d.description.into_raw(),
        })
        .collect();
    Box::into_raw(listed) as *mut PwsinkDevice
}

/// Frees an array pwsink_devices returned, with the count it gave. Does nothing for null.
///
/// # Safety
/// devices is null, or an array from pwsink_devices, with its count, not yet freed.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn pwsink_free_devices(devices: *mut PwsinkDevice, count: usize) {
    if devices.is_null() {
        return;
    }
    // SAFETY: the array and its strings were made by Box::into_raw and CString::into_raw in
    // pwsink_devices, with this length, and are freed only here.
    unsafe {
        let listed = Box::from_raw(std::ptr::slice_from_raw_parts_mut(devices, count));
        for d in listed.iter() {
            drop(CString::from_raw(d.name));
            drop(CString::from_raw(d.description));
        }
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
/// LINDA_PWSINK_TEST_HOOKS=1 was in the environment when the library started: anything else
/// that called it would hold up connecting, attaching and waking every stream.
#[unsafe(no_mangle)]
pub extern "C" fn pwsink_block_loop(ms: u64) {
    if let Ok(engine) = engine() {
        engine.block_loop(ms);
    }
}
