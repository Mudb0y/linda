//! One player's stream: the ring its producer fills and the real-time callback drains, the
//! position the player is told, and the clock that keeps that position moving whenever PipeWire
//! stops consuming.
//!
//! Three kinds of thread touch a stream, and each datum has one writer:
//! - the producer, the one thread at a time that NVDA's player lets call it, writes the ring
//!   ahead of the read position, `write_pos`, `base`, `paused`, the gains, the clock's fields and
//!   `kick_ns`, and raises `skip_to`;
//! - the real-time callback writes `read_pos`, `in_cycle`, the position snapshot and the cycle
//!   counters;
//! - the loop thread writes what it knows of the connection and the pw_stream: `attached_ns`,
//!   `active`, `connected`, `streaming`, `running`, `broken`, `retry_ns` and `pw`. It also clears
//!   the cycle counters, but only in `prepare_attach`, before there is a pw_stream whose callback
//!   could run.
//!
//! Nothing here takes a lock or waits. These are the orderings that matter:
//! - `stop` is one store of `paused`, which the callback loads at the start of its next cycle, so
//!   at most the cycle in progress is heard after it.
//! - The ring's bytes are published by the store of `write_pos`, which the callback loads before
//!   reading them.
//! - A reset or the clock raises `skip_to` while the callback may be inside a cycle that read the
//!   old value and is still copying the frames it covers. The producer reuses those frames only
//!   when it sees `in_cycle` false after raising `skip_to`; the callback sets `in_cycle` before it
//!   loads `skip_to`. Both pairs are sequentially consistent, so one of them sees the other.
//! - The loop thread deactivates a stream that has nothing to play while the producer may be
//!   writing to it. The producer stores `write_pos` and then loads `active`; the loop thread
//!   stores `active` false and then loads `write_pos`, again sequentially consistent: either the
//!   producer sees the stream inactive and asks for it to be woken, or the loop thread sees the
//!   new frames and keeps it active.
//! - The callback hands over audio only while `streaming` and `running` are both set, which the
//!   loop thread stores when PipeWire says so. Seeing them a cycle late costs a cycle of waiting,
//!   never a cycle of audio.

use std::ffi::{CString, c_void};
use std::ptr::{self, NonNull};
use std::sync::atomic::Ordering::{Acquire, Relaxed, Release, SeqCst};
use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU32, AtomicU64};

use crate::format::{Format, MAX_CHANNELS, Sample, apply_gain};

pub const NS: u64 = 1_000_000_000;

/// How far the producer may run ahead of the graph. NVDA's player keeps at most 400 ms queued,
/// so a second leaves room for what the graph holds besides.
const RING_SECONDS: u64 = 1;
/// How long a stream that the graph has been consuming may go without a cycle before it keeps
/// time by the clock, unless two cycles are longer.
const SCHEDULED_BOUND_NS: u64 = 50_000_000;
/// How long a stream waits for the graph to start consuming it, once after it is given a
/// pw_stream and once after each time it asks to be woken, before it keeps time by the clock. A new
/// stream waits for WirePlumber to link it and a woken one for the daemon to schedule it again,
/// which under load takes tens of milliseconds; waiting this long means neither loses its start,
/// and it is a wait paid once, not on every sound, when nothing is going to consume it.
const START_BOUND_NS: u64 = 500_000_000;
/// The longest gap between process calls taken as one cycle rather than as a pause in scheduling.
const MAX_CYCLE_NS: u64 = 200_000_000;
/// The least silence handed over after the last frames, more than PipeWire's resampler holds at
/// its default quality.
const MIN_DRAIN_FRAMES: u64 = 64;

pub struct Stream {
    pub format: Format,
    pub target: Option<CString>,
    stride: usize,
    capacity: u64,
    ring: NonNull<u8>,
    ring_len: usize,
    /// When the stream was made, from which a stream still waiting for its pw_stream waits for the
    /// graph.
    opened_ns: u64,

    write_pos: AtomicU64,
    /// Where the current stretch began, set by reset; positions are reported from here.
    base: AtomicU64,
    read_pos: AtomicU64,
    /// Everything before this is discarded, by reset or by the clock. Only ever raised.
    skip_to: AtomicU64,
    in_cycle: AtomicBool,
    paused: AtomicBool,
    gains: [AtomicU32; MAX_CHANNELS],

    snap_seq: AtomicU64,
    snap_graph_ns: AtomicU64,
    snap_pos: AtomicU64,
    snap_latency: AtomicU64,

    /// Cycles in which the stream could hand over audio, since it was last given a pw_stream.
    process_calls: AtomicU64,
    cycle_ns: AtomicU64,
    last_graph_ns: AtomicU64,
    /// Silence still to hand over after the last frames, so that none of them stays in PipeWire.
    drain_frames: AtomicU64,
    resampler_frames: AtomicU64,

    clock_pos: AtomicU64,
    clock_ns: AtomicU64,
    seen_calls: AtomicU64,
    /// When the producer last saw process_calls move.
    progress_ns: AtomicU64,
    /// When the producer last asked the loop thread to wake the stream.
    kick_ns: AtomicU64,
    played_high: AtomicU64,

    /// When the loop thread last gave the stream a pw_stream, or 0 while it has none.
    attached_ns: AtomicU64,
    active: AtomicBool,
    /// Whether the library had a connection when the loop thread last looked.
    connected: AtomicBool,
    /// The pw_stream is in its streaming state, which PipeWire enters once its links are set up.
    streaming: AtomicBool,
    /// The daemon reports the stream's node running, which it does only once the node is in the
    /// graph and the sinks it feeds wait for it in every cycle.
    running: AtomicBool,
    broken: AtomicBool,
    retry_ns: AtomicU64,
    pw: AtomicPtr<c_void>,
}

// SAFETY: the ring is written only ahead of the read position and read only behind the write
// position, which the atomics publish; everything else is an atomic.
unsafe impl Send for Stream {}
unsafe impl Sync for Stream {}

impl Drop for Stream {
    fn drop(&mut self) {
        // SAFETY: the pointer and length are those of the boxed slice made in new.
        drop(unsafe {
            Box::from_raw(ptr::slice_from_raw_parts_mut(
                self.ring.as_ptr(),
                self.ring_len,
            ))
        });
    }
}

impl Stream {
    pub fn new(format: Format, target: Option<CString>, now: u64, connected: bool) -> Stream {
        let stride = format.stride();
        let capacity = format.rate as u64 * RING_SECONDS;
        let ring_len = capacity as usize * stride;
        let ring = Box::into_raw(vec![0u8; ring_len].into_boxed_slice());
        Stream {
            format,
            target,
            stride,
            capacity,
            // SAFETY: Box::into_raw never returns null.
            ring: unsafe { NonNull::new_unchecked(ring as *mut u8) },
            ring_len,
            opened_ns: now,
            write_pos: AtomicU64::new(0),
            base: AtomicU64::new(0),
            read_pos: AtomicU64::new(0),
            skip_to: AtomicU64::new(0),
            in_cycle: AtomicBool::new(false),
            paused: AtomicBool::new(true),
            gains: std::array::from_fn(|_| AtomicU32::new(1.0f32.to_bits())),
            snap_seq: AtomicU64::new(0),
            snap_graph_ns: AtomicU64::new(0),
            snap_pos: AtomicU64::new(0),
            snap_latency: AtomicU64::new(0),
            process_calls: AtomicU64::new(0),
            cycle_ns: AtomicU64::new(0),
            last_graph_ns: AtomicU64::new(0),
            drain_frames: AtomicU64::new(0),
            resampler_frames: AtomicU64::new(0),
            clock_pos: AtomicU64::new(0),
            clock_ns: AtomicU64::new(0),
            seen_calls: AtomicU64::new(0),
            progress_ns: AtomicU64::new(0),
            kick_ns: AtomicU64::new(0),
            played_high: AtomicU64::new(0),
            attached_ns: AtomicU64::new(0),
            active: AtomicBool::new(false),
            connected: AtomicBool::new(connected),
            streaming: AtomicBool::new(false),
            running: AtomicBool::new(false),
            broken: AtomicBool::new(false),
            retry_ns: AtomicU64::new(0),
            pw: AtomicPtr::new(ptr::null_mut()),
        }
    }

    // The producer's side. NVDA's player holds its own lock around every call, so these never run
    // on two threads at once for one stream.

    /// Copies all the whole frames in data into the ring, or nothing when they do not all fit,
    /// and returns how many it took. Also says whether the loop thread must wake the stream.
    pub fn write(&self, data: &[u8], now: u64) -> (u64, bool) {
        self.tick(now);
        let wp = self.write_pos.load(Relaxed);
        let free = self.capacity - (wp - self.reclaim_point());
        let frames = (data.len() / self.stride) as u64;
        if frames > free {
            return (0, false);
        }
        let bytes = frames as usize * self.stride;
        let start = (wp % self.capacity) as usize * self.stride;
        let first = bytes.min(self.ring_len - start);
        // SAFETY: both runs lie within the ring, and in frames the callback will not read before
        // write_pos is published below, since they are at or beyond it and less than a ring's
        // length ahead of the reclaim point.
        unsafe {
            ptr::copy_nonoverlapping(data.as_ptr(), self.ring.as_ptr().add(start), first);
            ptr::copy_nonoverlapping(data.as_ptr().add(first), self.ring.as_ptr(), bytes - first);
        }
        self.write_pos.store(wp + frames, SeqCst);
        (frames, frames > 0 && self.needs_kick(now))
    }

    /// Frames written but not yet taken by the graph or discarded.
    pub fn buffered(&self, now: u64) -> u64 {
        self.tick(now);
        self.write_pos.load(Relaxed) - self.consumed()
    }

    /// Frames heard since the last reset: what the graph has played out, by its own clock, or what
    /// the wall clock has discarded while the graph did not consume. Never more than was written,
    /// and never less than it said before.
    pub fn played(&self, now: u64) -> u64 {
        self.tick(now);
        let wp = self.write_pos.load(Relaxed);
        let pos = self
            .heard(now)
            .max(self.skip_to.load(Acquire))
            .min(wp)
            .max(self.played_high.load(Relaxed));
        self.played_high.store(pos, Relaxed);
        pos - self.base.load(Relaxed)
    }

    /// Lets the callback give frames again from its next cycle. Says whether the loop thread must
    /// wake the stream.
    pub fn start(&self, now: u64) -> bool {
        if self.paused.swap(false, SeqCst) {
            self.restart_clock(now);
        }
        self.write_pos.load(Relaxed) > self.consumed() && self.needs_kick(now)
    }

    /// The callback gives nothing from its next cycle. A single store, so that it can never wait
    /// for anything: what the graph already holds is at most the cycle in progress.
    pub fn stop(&self) {
        self.paused.store(true, Release);
    }

    /// Discards everything written. The callback skips to here at its next cycle.
    pub fn reset(&self, now: u64) {
        let wp = self.write_pos.load(Relaxed);
        self.base.store(wp, Relaxed);
        self.played_high.store(wp, Relaxed);
        self.skip_to.fetch_max(wp, SeqCst);
        self.clock_pos.store(wp, Relaxed);
        self.clock_ns.store(now, Relaxed);
    }

    pub fn set_volume(&self, channel: u32, level: f32) -> bool {
        if channel >= self.format.channels || !(0.0..=1.0).contains(&level) {
            return false;
        }
        self.gains[channel as usize].store(level.to_bits(), Relaxed);
        true
    }

    /// Readies a stream taken from the pool for a new player: closing stopped and reset it.
    pub fn reuse(&self, now: u64) {
        for g in &self.gains {
            g.store(1.0f32.to_bits(), Relaxed);
        }
        self.restart_clock(now);
    }

    /// Moves the read position by the wall clock while the stream is started, holds frames and has
    /// not been consumed for longer than its bound, discarding at the stream's rate as a device
    /// would play. The discard goes through skip_to, never read_pos, which only the callback
    /// writes; when the graph comes back it continues from there, so nothing discarded is played
    /// late.
    fn tick(&self, now: u64) {
        let calls = self.process_calls.load(Acquire);
        if calls != self.seen_calls.load(Relaxed) {
            self.seen_calls.store(calls, Relaxed);
            self.progress_ns.store(now, Relaxed);
        }
        let wp = self.write_pos.load(Relaxed);
        let consumed = self.consumed();
        if self.paused.load(Relaxed)
            || consumed >= wp
            || consumed != self.clock_pos.load(Relaxed)
            || !self.stalled(now, calls)
        {
            // Nothing the clock should play, or the graph is doing it: the clock starts from here
            // and now if it is needed later, so that frames written after a dry spell play from
            // when they arrive.
            self.clock_pos.store(consumed, Relaxed);
            self.clock_ns.store(now, Relaxed);
            return;
        }
        let since = self.clock_ns.load(Relaxed);
        let frames =
            (now.saturating_sub(since) as u128 * self.format.rate as u128 / NS as u128) as u64;
        let target = (consumed + frames).min(wp);
        self.skip_to.fetch_max(target, SeqCst);
        self.clock_pos.store(target, Relaxed);
        if target == wp {
            self.clock_ns.store(now, Relaxed);
        } else {
            // Moved on by whole frames only, keeping the fraction of a frame already under way.
            let used = (frames as u128 * NS as u128 / self.format.rate as u128) as u64;
            self.clock_ns.store(since + used, Relaxed);
        }
    }

    /// Whether the graph has had long enough to consume the stream and has not.
    fn stalled(&self, now: u64, calls: u64) -> bool {
        let attached = self.attached_ns.load(Acquire);
        let (since, bound) = if attached == 0 {
            if !self.connected.load(Acquire) {
                // No daemon: keep time from the first write, as the null sink does.
                return true;
            }
            // The loop thread has yet to give it a pw_stream.
            (self.opened_ns, START_BOUND_NS)
        } else if calls == 0 {
            (attached, START_BOUND_NS)
        } else {
            let progress = self.progress_ns.load(Relaxed).max(attached);
            let kick = self.kick_ns.load(Relaxed);
            if kick > progress {
                (kick, START_BOUND_NS)
            } else {
                let cycles = 2 * self.cycle_ns.load(Relaxed);
                (progress, SCHEDULED_BOUND_NS.max(cycles))
            }
        };
        now > since + bound
    }

    fn restart_clock(&self, now: u64) {
        self.clock_pos.store(self.consumed(), Relaxed);
        self.clock_ns.store(now, Relaxed);
    }

    /// Whether the loop thread must wake the stream for frames just written or started.
    fn needs_kick(&self, now: u64) -> bool {
        if self.attached_ns.load(SeqCst) != 0 && !self.active.load(SeqCst) {
            self.kick_ns.store(now, Relaxed);
            return true;
        }
        false
    }

    /// The ring position before which the producer may write again. Frames the clock or a reset
    /// discarded can be reused only if the callback is not inside a cycle, in which it may have
    /// read skip_to before it was raised and still be copying them; if it starts a cycle after
    /// this looked, it sees the raised skip_to and never reads them.
    fn reclaim_point(&self) -> u64 {
        let rp = self.read_pos.load(Acquire);
        let skip = self.skip_to.load(SeqCst);
        if skip > rp && !self.in_cycle.load(SeqCst) {
            skip
        } else {
            rp
        }
    }

    fn heard(&self, now: u64) -> u64 {
        let (graph_ns, pos, latency) = self.snapshot();
        if pos == 0 {
            return 0;
        }
        let elapsed = now.saturating_sub(graph_ns) as f64 * self.format.rate as f64 / NS as f64;
        let behind = (latency - elapsed).max(0.0);
        (pos as f64 - behind).max(0.0) as u64
    }

    fn snapshot(&self) -> (u64, u64, f64) {
        let mut spins = 0u32;
        loop {
            let a = self.snap_seq.load(Acquire);
            if a.is_multiple_of(2) {
                let graph_ns = self.snap_graph_ns.load(Relaxed);
                let pos = self.snap_pos.load(Relaxed);
                let latency = f64::from_bits(self.snap_latency.load(Relaxed));
                std::sync::atomic::fence(Acquire);
                if self.snap_seq.load(Relaxed) == a {
                    return (graph_ns, pos, latency);
                }
            }
            // The callback is between two stores; it never waits, so this is a few nanoseconds
            // unless the system preempted it there.
            spins += 1;
            if spins < 64 {
                std::hint::spin_loop();
            } else {
                std::thread::yield_now();
            }
        }
    }

    // Shared reads.

    pub fn consumed(&self) -> u64 {
        self.read_pos.load(Acquire).max(self.skip_to.load(Acquire))
    }

    /// Whether the graph has nothing left to play.
    pub fn is_empty(&self) -> bool {
        self.write_pos.load(SeqCst) <= self.consumed()
    }

    /// How long what the stream holds will take to play, or None while it is paused.
    pub fn pending_ns(&self) -> Option<u64> {
        if self.paused.load(Relaxed) {
            return None;
        }
        let frames = self.write_pos.load(Relaxed).saturating_sub(self.consumed());
        Some((frames as u128 * NS as u128 / self.format.rate as u128) as u64)
    }

    /// Whether the callback has handed over the silence that follows the last frames, which a
    /// stream must have done before it is deactivated: what PipeWire still held would otherwise
    /// play when it is next activated.
    pub fn is_drained(&self) -> bool {
        self.drain_frames.load(Acquire) == 0
    }

    pub fn process_calls(&self) -> u64 {
        self.process_calls.load(Acquire)
    }

    /// Frames PipeWire's resampler held in the last cycle, which play after anything handed over.
    pub fn resampler_frames(&self) -> u64 {
        self.resampler_frames.load(Relaxed)
    }

    pub fn is_attached(&self) -> bool {
        self.attached_ns.load(Acquire) != 0
    }

    pub fn is_active(&self) -> bool {
        self.active.load(SeqCst)
    }

    pub fn pw(&self) -> *mut c_void {
        self.pw.load(Acquire)
    }

    // The loop thread's side.

    /// Clears what the callback counted for the previous pw_stream, before a new one is made.
    pub fn prepare_attach(&self) {
        self.process_calls.store(0, Release);
        self.last_graph_ns.store(0, Relaxed);
        self.cycle_ns.store(0, Relaxed);
        self.streaming.store(false, Release);
        self.running.store(false, Release);
    }

    pub fn set_pw(&self, pw: *mut c_void) {
        self.pw.store(pw, Release);
    }

    /// The stream has a connected pw_stream, which is active.
    pub fn mark_attached(&self, now: u64) {
        self.active.store(true, SeqCst);
        self.attached_ns.store(now.max(1), SeqCst);
    }

    pub fn mark_detached(&self) {
        self.attached_ns.store(0, SeqCst);
        self.active.store(false, SeqCst);
        self.streaming.store(false, Release);
        self.running.store(false, Release);
        self.pw.store(ptr::null_mut(), Release);
    }

    pub fn set_connected(&self, connected: bool) {
        self.connected.store(connected, Release);
    }

    pub fn set_streaming(&self, streaming: bool) {
        self.streaming.store(streaming, Release);
    }

    pub fn set_running(&self, running: bool) {
        self.running.store(running, Release);
    }

    pub fn set_broken(&self, broken: bool) {
        self.broken.store(broken, Relaxed);
    }

    pub fn take_broken(&self) -> bool {
        self.broken.swap(false, Relaxed)
    }

    pub fn retry_ns(&self) -> u64 {
        self.retry_ns.load(Relaxed)
    }

    pub fn set_retry_ns(&self, at: u64) {
        self.retry_ns.store(at, Relaxed);
    }

    /// Marks the stream inactive unless something was written since the caller found it empty,
    /// and says whether it did; the caller then deactivates its pw_stream. A write racing this
    /// either sees the stream inactive and asks for it to be woken, or is seen here.
    pub fn deactivate_if_still_empty(&self) -> bool {
        self.active.store(false, SeqCst);
        if self.is_empty() {
            return true;
        }
        self.active.store(true, SeqCst);
        false
    }

    pub fn reactivate(&self) {
        self.active.store(true, SeqCst);
    }

    // The real-time callback's side: no allocation, no lock, no system call, no wait.

    /// Whether PipeWire will mix what the callback hands over in this cycle.
    pub fn ready(&self) -> bool {
        self.streaming.load(Acquire) && self.running.load(Acquire)
    }

    /// Enters a cycle and returns the read position, having skipped what was discarded.
    pub fn begin_cycle(&self) -> u64 {
        self.in_cycle.store(true, SeqCst);
        let skip = self.skip_to.load(SeqCst);
        let rp = self.read_pos.load(Relaxed);
        if skip > rp {
            self.read_pos.store(skip, Release);
            skip
        } else {
            rp
        }
    }

    pub fn available(&self, rp: u64) -> u64 {
        if self.paused.load(Acquire) {
            0
        } else {
            self.write_pos.load(Acquire).saturating_sub(rp)
        }
    }

    pub fn stride(&self) -> usize {
        self.stride
    }

    /// Copies the frames from rp that fill dst, then applies the channel gains and the process
    /// gain.
    pub fn copy_out(&self, rp: u64, dst: &mut [u8], process_gain: f32) {
        let bytes = dst.len();
        let start = (rp % self.capacity) as usize * self.stride;
        let first = bytes.min(self.ring_len - start);
        // SAFETY: the runs lie within the ring and behind write_pos, which was loaded with Acquire
        // after the producer stored it with its copies done.
        unsafe {
            ptr::copy_nonoverlapping(self.ring.as_ptr().add(start), dst.as_mut_ptr(), first);
            ptr::copy_nonoverlapping(
                self.ring.as_ptr(),
                dst.as_mut_ptr().add(first),
                bytes - first,
            );
        }
        let mut gains = [1.0f32; MAX_CHANNELS];
        for (g, stored) in gains
            .iter_mut()
            .zip(&self.gains)
            .take(self.format.channels as usize)
        {
            *g = f32::from_bits(stored.load(Relaxed)) * process_gain;
        }
        apply_gain(
            self.format.sample,
            self.format.channels as usize,
            &gains,
            dst,
        );
    }

    pub fn end_cycle(&self, rp: u64) {
        self.read_pos.store(rp, Release);
        self.in_cycle.store(false, SeqCst);
    }

    /// Publishes where the graph's read position was in the cycle at graph_ns and how many of
    /// those frames had not yet been heard.
    pub fn publish(&self, graph_ns: u64, pos: u64, latency: f64) {
        let seq = self.snap_seq.load(Relaxed);
        self.snap_seq.store(seq + 1, Relaxed);
        std::sync::atomic::fence(Release);
        self.snap_graph_ns.store(graph_ns, Relaxed);
        self.snap_pos.store(pos, Relaxed);
        self.snap_latency.store(latency.to_bits(), Relaxed);
        self.snap_seq.store(seq + 2, Release);
    }

    /// Whether silence is still due after the last frames handed over.
    pub fn draining(&self) -> bool {
        self.drain_frames.load(Relaxed) > 0
    }

    pub fn silence_byte(&self) -> u8 {
        if self.format.sample == Sample::U8 {
            0x80
        } else {
            0
        }
    }

    /// Counts a buffer handed over: frames from the ring, then silence up to filled.
    pub fn note_handed(&self, frames: u64, filled: u64) {
        if frames > 0 {
            let drain = MIN_DRAIN_FRAMES.max(2 * self.resampler_frames.load(Relaxed));
            self.drain_frames.store(drain, Release);
        } else {
            let left = self.drain_frames.load(Relaxed).saturating_sub(filled);
            self.drain_frames.store(left, Release);
        }
    }

    /// Counts a cycle, with the frames PipeWire's resampler said it held.
    pub fn note_cycle(&self, graph_ns: u64, resampler_frames: u64) {
        let last = self.last_graph_ns.swap(graph_ns, Relaxed);
        if last != 0 && graph_ns > last && graph_ns - last <= MAX_CYCLE_NS {
            self.cycle_ns.store(graph_ns - last, Relaxed);
        }
        self.resampler_frames.store(resampler_frames, Relaxed);
        self.process_calls.fetch_add(1, Release);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MS: u64 = 1_000_000;

    fn mono16(rate: u32) -> Stream {
        Stream::new(Format::from_wave(1, 1, rate, 16).unwrap(), None, 1, false)
    }

    fn frames(n: usize, value: i16) -> Vec<u8> {
        std::iter::repeat_n(value.to_le_bytes(), n)
            .flatten()
            .collect()
    }

    /// One cycle of the real-time callback, without PipeWire: takes up to n frames.
    fn cycle(s: &Stream, n: u64, graph_ns: u64) -> Vec<u8> {
        let rp = s.begin_cycle();
        let take = s.available(rp).min(n);
        let mut out = vec![0u8; take as usize * s.stride()];
        s.copy_out(rp, &mut out, 1.0);
        s.end_cycle(rp + take);
        if take > 0 {
            s.publish(graph_ns, rp + take, 0.0);
        }
        s.note_cycle(graph_ns, 0);
        out
    }

    /// What the loop thread does when it gives a stream a pw_stream that is linked and running.
    fn attach(s: &Stream, now: u64) {
        s.set_connected(true);
        s.prepare_attach();
        s.mark_attached(now);
        s.set_streaming(true);
        s.set_running(true);
    }

    #[test]
    fn frames_come_out_as_they_went_in_across_the_wrap() {
        let s = mono16(8000);
        attach(&s, 1);
        s.start(1);
        let mut all = Vec::new();
        for i in 0..30 {
            let chunk: Vec<u8> = (0..700i16)
                .flat_map(|v| (v + i * 700).to_le_bytes())
                .collect();
            assert_eq!(s.write(&chunk, 2).0, 700);
            all.extend_from_slice(&cycle(&s, 700, 3 + i as u64));
        }
        let expected: Vec<u8> = (0..21000i32)
            .flat_map(|v| (v as i16).to_le_bytes())
            .collect();
        assert_eq!(all, expected);
    }

    #[test]
    fn a_write_that_does_not_fit_takes_nothing() {
        let s = mono16(8000);
        attach(&s, 1);
        assert_eq!(s.write(&frames(9000, 1), 1).0, 0);
        assert_eq!(s.buffered(1), 0);
        assert_eq!(s.write(&frames(8000, 1), 1).0, 8000);
        assert_eq!(s.write(&frames(1, 1), 1).0, 0);
        assert_eq!(s.buffered(1), 8000);
    }

    #[test]
    fn a_stop_gives_nothing_from_the_next_cycle() {
        let s = mono16(8000);
        attach(&s, 1);
        s.write(&frames(4000, 7), 1);
        s.start(1);
        assert_eq!(cycle(&s, 100, 10).len(), 200);
        s.stop();
        assert!(cycle(&s, 100, 20).is_empty());
        s.start(30);
        assert_eq!(cycle(&s, 100, 30).len(), 200);
    }

    #[test]
    fn frames_a_cycle_may_still_read_are_not_reused() {
        let s = mono16(8000);
        attach(&s, 1);
        s.write(&frames(8000, 1), 1);
        s.start(1);
        // The callback enters a cycle and may be copying from 0 when the producer resets.
        let rp = s.begin_cycle();
        s.stop();
        s.reset(2);
        assert_eq!(s.write(&frames(100, 2), 3).0, 0, "the ring is still in use");
        s.end_cycle(rp);
        assert_eq!(s.write(&frames(100, 2), 4).0, 100);
        s.start(4);
        let out = cycle(&s, 200, 5);
        assert_eq!(out, frames(100, 2));
    }

    #[test]
    fn with_no_connection_a_stream_keeps_time_from_its_first_write() {
        let s = mono16(8000);
        let t0 = 1000 * MS;
        s.write(&frames(3200, 1), t0);
        s.start(t0);
        assert_eq!(s.played(t0), 0);
        assert_eq!(s.played(t0 + 100 * MS), 800);
        assert_eq!(s.buffered(t0 + 100 * MS), 2400);
        assert_eq!(s.played(t0 + 400 * MS), 3200);
        assert_eq!(s.played(t0 + 900 * MS), 3200, "never more than was written");
        // Dry since then: what comes next plays from when it arrives.
        s.write(&frames(800, 1), t0 + 2000 * MS);
        assert_eq!(s.played(t0 + 2000 * MS), 3200);
        assert_eq!(s.played(t0 + 2050 * MS), 3600);
        assert_eq!(s.played(t0 + 2100 * MS), 4000);
    }

    #[test]
    fn a_stream_waiting_for_its_pw_stream_waits_while_connected() {
        let t0 = 1000 * MS;
        let s = Stream::new(Format::from_wave(1, 1, 8000, 16).unwrap(), None, t0, true);
        s.write(&frames(3200, 1), t0);
        s.start(t0);
        assert_eq!(
            s.played(t0 + 80 * MS),
            0,
            "the loop thread is busy, not gone"
        );
        assert_eq!(s.played(t0 + 450 * MS), 0);
        assert!(s.played(t0 + 600 * MS) > 0);
    }

    #[test]
    fn a_stopped_stream_does_not_move() {
        let s = mono16(8000);
        let t0 = 1000 * MS;
        s.write(&frames(3200, 1), t0);
        s.start(t0);
        assert_eq!(s.played(t0 + 100 * MS), 800);
        s.stop();
        assert_eq!(s.played(t0 + 300 * MS), 800);
        s.start(t0 + 300 * MS);
        assert_eq!(s.played(t0 + 400 * MS), 1600);
    }

    #[test]
    fn a_new_stream_waits_for_linking_once() {
        let s = mono16(8000);
        let t0 = 1000 * MS;
        attach(&s, t0);
        s.set_running(false);
        s.write(&frames(3200, 1), t0);
        s.start(t0);
        assert_eq!(s.played(t0 + 300 * MS), 0);
        assert_eq!(s.played(t0 + 500 * MS), 0);
        // Past the bound, time counts from the last look that found it within bounds.
        assert_eq!(s.played(t0 + 600 * MS), 800);
        assert_eq!(s.played(t0 + 700 * MS), 1600);
        // Stopped, reset and started again, as nvwave does between utterances: still never
        // scheduled, the stream keeps time at once rather than waiting again.
        let t1 = t0 + 2000 * MS;
        s.stop();
        s.reset(t1);
        s.write(&frames(800, 1), t1);
        s.start(t1);
        assert_eq!(s.played(t1 + 50 * MS), 400);
        assert_eq!(s.played(t1 + 100 * MS), 800);
    }

    #[test]
    fn a_scheduled_stream_that_stops_being_consumed_keeps_time() {
        let s = mono16(8000);
        let t0 = 1000 * MS;
        attach(&s, t0);
        s.write(&frames(4000, 1), t0);
        s.start(t0);
        for i in 0..10 {
            cycle(&s, 80, t0 + i * 10 * MS);
            s.played(t0 + i * 10 * MS);
        }
        let consumed = s.consumed();
        assert_eq!(consumed, 800);
        // The graph stops. Within the bound nothing moves.
        assert_eq!(s.buffered(t0 + 130 * MS), 3200);
        // Past it, the clock discards in real time from the last look within the bound.
        assert_eq!(s.buffered(t0 + 230 * MS), 3200 - 800);
        assert_eq!(s.played(t0 + 330 * MS), 2400);
        // When the graph comes back it continues from where the clock got to.
        let rp = s.begin_cycle();
        assert_eq!(rp, 2400);
        s.end_cycle(rp);
    }

    #[test]
    fn a_woken_stream_waits_for_the_graph_once_per_wake() {
        let s = mono16(8000);
        let t0 = 1000 * MS;
        attach(&s, t0);
        s.write(&frames(80, 1), t0);
        s.start(t0);
        cycle(&s, 80, t0);
        s.played(t0);
        // Deactivated by the loop thread while dry, then written to.
        assert!(s.deactivate_if_still_empty());
        let t1 = t0 + 1000 * MS;
        assert!(s.write(&frames(3200, 1), t1).1, "asks to be woken");
        // The loop thread and the daemon are slow to bring it back: nothing is discarded meanwhile.
        assert_eq!(s.buffered(t1 + 100 * MS), 3200);
        assert_eq!(s.buffered(t1 + 450 * MS), 3200);
        s.reactivate();
        // Never brought back: the clock takes over once the wait is over, and does not wait again
        // for writes that need no wake.
        assert!(s.buffered(t1 + 600 * MS) < 3200);
        assert!(!s.write(&frames(10, 1), t1 + 600 * MS).1);
        assert_eq!(s.buffered(t1 + 1100 * MS), 0);
    }

    #[test]
    fn the_bound_scales_with_long_cycles() {
        let s = mono16(8000);
        let t0 = 1000 * MS;
        attach(&s, t0);
        s.write(&frames(4000, 1), t0);
        s.start(t0);
        cycle(&s, 400, t0);
        cycle(&s, 400, t0 + 80 * MS);
        s.played(t0 + 80 * MS);
        assert_eq!(
            s.buffered(t0 + 230 * MS),
            3200,
            "two 80 ms cycles have not passed"
        );
        assert!(s.buffered(t0 + 300 * MS) < 3200);
    }

    #[test]
    fn played_is_what_the_graph_has_played_out() {
        let s = mono16(8000);
        attach(&s, 1);
        s.write(&frames(800, 1), 1);
        s.start(1);
        let rp = s.begin_cycle();
        s.end_cycle(rp + 400);
        s.publish(10 * MS, 400, 80.0);
        s.note_cycle(10 * MS, 0);
        assert_eq!(s.played(10 * MS), 320);
        assert_eq!(s.played(15 * MS), 360);
        assert_eq!(s.played(30 * MS), 400);
    }

    #[test]
    fn a_reset_counts_from_zero_and_discards() {
        let s = mono16(8000);
        attach(&s, 1);
        s.write(&frames(800, 1), 1);
        s.start(1);
        cycle(&s, 400, 10 * MS);
        s.stop();
        s.reset(20 * MS);
        assert_eq!(s.played(20 * MS), 0);
        assert_eq!(s.buffered(20 * MS), 0);
        s.write(&frames(100, 3), 20 * MS);
        s.start(20 * MS);
        assert_eq!(cycle(&s, 400, 30 * MS), frames(100, 3));
    }

    #[test]
    fn a_write_to_an_inactive_stream_asks_for_it_to_be_woken() {
        let s = mono16(8000);
        assert!(!s.write(&frames(10, 1), 1).1, "detached: nothing to wake");
        attach(&s, 1);
        assert!(!s.write(&frames(10, 1), 1).1);
        s.active.store(false, SeqCst);
        assert!(s.write(&frames(10, 1), 1).1);
    }

    #[test]
    fn a_write_racing_deactivation_is_never_left_in_an_inactive_stream() {
        // The loop thread found the stream empty, and the producer writes before it clears active:
        // the producer sees the stream still active and asks for nothing, so the loop thread must
        // see the frames and keep it active.
        let s = mono16(8000);
        attach(&s, 1);
        assert!(s.is_empty());
        let (_, kick) = s.write(&frames(10, 1), 2);
        let deactivated = s.deactivate_if_still_empty();
        assert!(
            kick || !deactivated,
            "frames left in an inactive stream with no wake asked for"
        );
        assert!(s.is_active());
        // The producer writes after active was cleared: it must ask for the stream to be woken.
        let s = mono16(8000);
        attach(&s, 1);
        assert!(s.deactivate_if_still_empty());
        assert!(s.write(&frames(10, 1), 3).1);
    }

    #[test]
    fn nothing_is_handed_over_until_the_graph_mixes_it() {
        let s = mono16(8000);
        s.set_connected(true);
        s.prepare_attach();
        s.mark_attached(1);
        assert!(!s.ready());
        s.set_streaming(true);
        assert!(!s.ready(), "streaming, but not yet in the graph");
        s.set_running(true);
        assert!(s.ready());
        s.mark_detached();
        assert!(!s.ready());
    }

    #[test]
    fn silence_follows_the_last_frames_until_the_resampler_is_drained() {
        let s = mono16(8000);
        assert!(!s.draining());
        s.note_cycle(1, 30);
        s.note_handed(100, 128);
        assert!(s.draining() && !s.is_drained());
        s.note_handed(0, 40);
        assert!(
            s.draining(),
            "40 frames of silence are fewer than the 64 due"
        );
        s.note_handed(0, 40);
        assert!(!s.draining() && s.is_drained());
        assert_eq!(s.silence_byte(), 0);
        let u8 = Stream::new(Format::from_wave(1, 1, 8000, 8).unwrap(), None, 1, false);
        assert_eq!(u8.silence_byte(), 0x80);
    }

    #[test]
    fn volumes_outside_the_stream_are_refused() {
        let s = mono16(8000);
        assert!(s.set_volume(0, 0.5));
        assert!(!s.set_volume(1, 0.5));
        assert!(!s.set_volume(0, 1.5));
        assert!(!s.set_volume(0, f32::NAN));
    }
}
