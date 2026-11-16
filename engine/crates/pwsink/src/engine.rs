//! The process's one PipeWire connection and what is made on it.
//!
//! A pw_thread_loop runs the connection. Everything that calls into PipeWire, other than the
//! real-time callback, runs on that loop's thread: making the context and connecting, giving
//! each stream a pw_stream, activating and deactivating them, and tearing it all down when the
//! daemon goes and connecting again a second later. The threads that call in from Python never
//! take the loop's lock and never wait for the daemon. They hand work over through a mutex that
//! the loop thread holds only to copy a list, and wake it through an eventfd.

use std::cell::UnsafeCell;
use std::collections::VecDeque;
use std::ffi::{CString, c_char, c_int, c_void};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::ptr;
use std::sync::atomic::Ordering::{Acquire, Relaxed, Release};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};

use pipewire as pw;
use pw::spa;
use pw::spa::spa_interface_call_method;
use pw::sys as pws;
use pwsink_core::format::Format;
use pwsink_core::stream::{NS, Stream};
use spa::sys as spas;

use crate::mono_ns;

/// How long after failing to connect, or after losing the daemon, the next attempt is made. A
/// failed attempt costs the loop thread a few milliseconds.
const RECONNECT_NS: u64 = NS;
const POOL_MAX: usize = 8;
const POOL_IDLE_NS: u64 = 60 * NS;
/// How often streams that cannot be deactivated yet are looked at again, since nothing wakes the
/// loop when they could be.
const RECHECK_NS: u64 = NS;
/// The time the callback takes to hand over the silence after a stream's last frames: a cycle or
/// two of the longest quantum.
const DRAIN_CHECK_NS: u64 = 100_000_000;

/// The gain every stream is multiplied by, as f32 bits.
pub(crate) static PROCESS_GAIN: AtomicU32 = AtomicU32::new(0x3f80_0000);
/// Frames taken by the graph, over every stream since the library was loaded.
pub(crate) static DELIVERED: AtomicU64 = AtomicU64::new(0);

/// A stream the loop thread has given a pw_stream, with the listeners on it. Boxed, since PipeWire
/// keeps the addresses of the hooks and event tables.
struct Link {
    stream: Arc<Stream>,
    hook: spas::spa_hook,
    events: pws::pw_stream_events,
    /// The stream's node, bound to learn when the daemon has it running.
    node: *mut pws::pw_proxy,
    node_hook: spas::spa_hook,
    node_events: pws::pw_node_events,
}

struct Shared {
    /// Every stream that is open or pooled.
    streams: Vec<Arc<Stream>>,
    /// Closed streams kept for reuse, oldest first, each with when it was closed.
    pool: VecDeque<(Arc<Stream>, u64)>,
    /// Streams to destroy.
    doomed: Vec<Arc<Stream>>,
}

struct LoopState {
    context: *mut pws::pw_context,
    core: *mut pws::pw_core,
    core_hook: spas::spa_hook,
    core_events: pws::pw_core_events,
    core_lost: bool,
    registry: *mut pws::pw_registry,
    next_connect_ns: u64,
    timer: *mut spas::spa_source,
    #[allow(
        clippy::vec_box,
        reason = "PipeWire keeps the addresses of each link's hooks, which must not move as the list grows"
    )]
    links: Vec<Box<Link>>,
}

pub(crate) struct Engine {
    thread_loop: *mut pws::pw_thread_loop,
    wake: OwnedFd,
    shared: Mutex<Shared>,
    keep_awake_until: AtomicU64,
    connected: AtomicBool,
    attached_count: AtomicUsize,
    pool_size: AtomicUsize,
    pool_max: AtomicUsize,
    pool_idle_ns: AtomicU64,
    block_ms: AtomicU64,
    state: UnsafeCell<LoopState>,
}

// SAFETY: thread_loop is used only while starting and by the loop thread; state only on the loop
// thread, inside its callbacks; the rest are atomics, a mutex and a file descriptor.
unsafe impl Send for Engine {}
unsafe impl Sync for Engine {}

static ENGINE: OnceLock<Result<&'static Engine, c_int>> = OnceLock::new();

/// The engine, started on first use. Starting it creates the loop thread and asks it to connect,
/// without waiting for that.
pub(crate) fn engine() -> Result<&'static Engine, c_int> {
    *ENGINE.get_or_init(|| unsafe { Engine::start() })
}

fn last_errno(fallback: c_int) -> c_int {
    match std::io::Error::last_os_error().raw_os_error() {
        Some(e) if e != 0 => -e,
        _ => -fallback,
    }
}

impl Engine {
    unsafe fn start() -> Result<&'static Engine, c_int> {
        pw::init();
        // SAFETY: plain system calls and PipeWire constructors; the loop is not running yet, so
        // its sources can be added without its lock.
        unsafe {
            let fd = libc::eventfd(0, libc::EFD_CLOEXEC | libc::EFD_NONBLOCK);
            if fd < 0 {
                return Err(last_errno(libc::EIO));
            }
            let wake = OwnedFd::from_raw_fd(fd);
            let thread_loop = pws::pw_thread_loop_new(c"pwsink".as_ptr(), ptr::null());
            if thread_loop.is_null() {
                return Err(last_errno(libc::ENOMEM));
            }
            let engine: &'static Engine = Box::leak(Box::new(Engine {
                thread_loop,
                wake,
                shared: Mutex::new(Shared {
                    streams: Vec::new(),
                    pool: VecDeque::new(),
                    doomed: Vec::new(),
                }),
                keep_awake_until: AtomicU64::new(0),
                connected: AtomicBool::new(false),
                attached_count: AtomicUsize::new(0),
                pool_size: AtomicUsize::new(0),
                pool_max: AtomicUsize::new(POOL_MAX),
                pool_idle_ns: AtomicU64::new(POOL_IDLE_NS),
                block_ms: AtomicU64::new(0),
                state: UnsafeCell::new(LoopState {
                    context: ptr::null_mut(),
                    core: ptr::null_mut(),
                    core_hook: std::mem::zeroed(),
                    core_events: std::mem::zeroed(),
                    core_lost: false,
                    registry: ptr::null_mut(),
                    next_connect_ns: 0,
                    timer: ptr::null_mut(),
                    links: Vec::new(),
                }),
            }));
            let data = engine as *const Engine as *mut c_void;
            let utils = (*pws::pw_thread_loop_get_loop(thread_loop)).utils;
            let io = spa_interface_call_method!(
                utils,
                spas::spa_loop_utils_methods,
                add_io,
                fd,
                spas::SPA_IO_IN,
                false,
                Some(on_wake),
                data
            );
            let timer = spa_interface_call_method!(
                utils,
                spas::spa_loop_utils_methods,
                add_timer,
                Some(on_timer),
                data
            );
            if io.is_null() || timer.is_null() {
                return Err(-libc::ENOMEM);
            }
            (*engine.state.get()).timer = timer;
            let r = pws::pw_thread_loop_start(thread_loop);
            if r < 0 {
                return Err(r);
            }
            engine.signal();
            Ok(engine)
        }
    }

    /// Wakes the loop thread. A write to an eventfd, which never blocks.
    pub(crate) fn signal(&self) {
        let one: u64 = 1;
        // SAFETY: writes eight bytes from a local to a descriptor this engine owns.
        unsafe {
            libc::write(
                self.wake.as_raw_fd(),
                &one as *const u64 as *const c_void,
                8,
            )
        };
    }

    fn shared(&self) -> MutexGuard<'_, Shared> {
        // Nothing panics while holding it, but a poisoned list is still a usable list.
        self.shared.lock().unwrap_or_else(|e| e.into_inner())
    }

    // Called from Python threads. None of these waits for the loop thread or the daemon.

    /// A pooled stream of this format and target, if one is waiting.
    pub(crate) fn take_pooled(
        &self,
        format: &Format,
        target: &Option<CString>,
    ) -> Option<Arc<Stream>> {
        let mut shared = self.shared();
        // The most recently closed, which is the likeliest still to be active.
        let i = shared
            .pool
            .iter()
            .rposition(|(s, _)| s.format == *format && s.target == *target)?;
        let (stream, _) = shared.pool.remove(i)?;
        self.pool_size.store(shared.pool.len(), Relaxed);
        Some(stream)
    }

    pub(crate) fn register(&self, stream: Arc<Stream>) {
        self.shared().streams.push(stream);
        self.signal();
    }

    /// Keeps a closed stream for reuse, destroying the oldest pooled one beyond the limit.
    pub(crate) fn pool_put(&self, stream: Arc<Stream>, now: u64) {
        let mut shared = self.shared();
        shared.pool.push_back((stream, now));
        let max = self.pool_max.load(Relaxed);
        evict(&mut shared, |pool| pool.len() > max);
        self.pool_size.store(shared.pool.len(), Relaxed);
        drop(shared);
        self.signal();
    }

    pub(crate) fn keep_awake(&self, ms: u64, now: u64) {
        let until = now.saturating_add(ms.saturating_mul(1_000_000));
        let before = self.keep_awake_until.swap(until, Release);
        if until < before {
            self.signal();
        }
    }

    pub(crate) fn set_pool_limits(&self, max: usize, idle_ms: u64) {
        self.pool_max.store(max, Relaxed);
        self.pool_idle_ns
            .store(idle_ms.saturating_mul(1_000_000), Relaxed);
        self.signal();
    }

    pub(crate) fn block_loop(&self, ms: u64) {
        self.block_ms.store(ms, Relaxed);
        self.signal();
    }

    pub(crate) fn connected(&self) -> bool {
        self.connected.load(Relaxed)
    }

    pub(crate) fn attached_count(&self) -> usize {
        self.attached_count.load(Relaxed)
    }

    pub(crate) fn pool_size(&self) -> usize {
        self.pool_size.load(Relaxed)
    }

    // The loop thread.

    /// Brings everything up to date: reconnects, attaches, detaches, expires the pool, and
    /// activates or deactivates streams, then arms the timer for the next thing that falls due.
    ///
    /// # Safety
    /// Runs only on the loop thread, inside one of its callbacks.
    unsafe fn service(&self) {
        // SAFETY: only the loop thread reaches the loop state.
        let lp = unsafe { &mut *self.state.get() };
        let block = self.block_ms.swap(0, Relaxed);
        if block > 0 {
            std::thread::sleep(std::time::Duration::from_millis(block));
        }
        let now = mono_ns();
        if lp.core_lost {
            unsafe { self.disconnect(lp) };
            lp.next_connect_ns = now + RECONNECT_NS;
        }
        let (streams, doomed, pool_due) = {
            let mut shared = self.shared();
            let idle = self.pool_idle_ns.load(Relaxed);
            let max = self.pool_max.load(Relaxed);
            evict(&mut shared, |pool| {
                pool.len() > max || pool.front().is_some_and(|(_, at)| at + idle <= now)
            });
            self.pool_size.store(shared.pool.len(), Relaxed);
            let due = shared.pool.front().map(|(_, at)| at + idle);
            (
                shared.streams.clone(),
                std::mem::take(&mut shared.doomed),
                due,
            )
        };
        for s in &doomed {
            unsafe { self.detach(lp, s) };
        }
        drop(doomed);
        if lp.core.is_null() && now >= lp.next_connect_ns {
            if lp.context.is_null() {
                unsafe { self.make_context(lp) };
            }
            if lp.context.is_null() {
                lp.next_connect_ns = now + RECONNECT_NS;
            } else {
                unsafe { self.connect(lp, now) };
            }
        }
        let mut retry_due = None::<u64>;
        if !lp.core.is_null() {
            for s in &streams {
                if s.take_broken() {
                    unsafe { self.detach(lp, s) };
                    s.set_retry_ns(now + RECONNECT_NS);
                }
                if !s.is_attached() {
                    if s.retry_ns() <= now {
                        unsafe { self.attach(lp, s, now) };
                    }
                    if !s.is_attached() {
                        let retry = s.retry_ns();
                        retry_due = Some(retry_due.map_or(retry, |d| d.min(retry)));
                    }
                }
            }
        }
        for link in lp.links.iter_mut() {
            unsafe { watch_node(lp.registry, link) };
        }
        let activity_due = unsafe { self.update_activity(lp, now) };
        let connect_due = lp.core.is_null().then_some(lp.next_connect_ns);
        let next = [pool_due, retry_due, activity_due, connect_due]
            .into_iter()
            .flatten()
            .min();
        unsafe { self.arm_timer(lp, next, now) };
    }

    unsafe fn make_context(&self, lp: &mut LoopState) {
        // SAFETY: on the loop thread; the properties are handed to the context.
        unsafe {
            let props = pws::pw_properties_new(ptr::null());
            set(props, c"application.name", c"Basedreader");
            set(props, c"application.id", c"basedreader");
            lp.context =
                pws::pw_context_new(pws::pw_thread_loop_get_loop(self.thread_loop), props, 0);
        }
    }

    unsafe fn connect(&self, lp: &mut LoopState, now: u64) {
        // SAFETY: on the loop thread, with a context; the hook and events live in the loop state,
        // which is never moved.
        unsafe {
            let core = pws::pw_context_connect(lp.context, ptr::null_mut(), 0);
            if core.is_null() {
                lp.next_connect_ns = now + RECONNECT_NS;
                return;
            }
            lp.core_events = std::mem::zeroed();
            lp.core_events.version = pws::PW_VERSION_CORE_EVENTS;
            lp.core_events.error = Some(on_core_error);
            lp.core_hook = std::mem::zeroed();
            spa_interface_call_method!(
                core,
                pws::pw_core_methods,
                add_listener,
                &mut lp.core_hook,
                &lp.core_events,
                self as *const Engine as *mut c_void
            );
            lp.registry = spa_interface_call_method!(
                core,
                pws::pw_core_methods,
                get_registry,
                pws::PW_VERSION_REGISTRY,
                0
            );
            lp.core = core;
            lp.core_lost = false;
        }
        self.connected.store(true, Relaxed);
    }

    /// Drops the connection and every pw_stream on it. Their rings and positions stay, so the
    /// streams keep time by the clock until they are attached again.
    unsafe fn disconnect(&self, lp: &mut LoopState) {
        let streams: Vec<Arc<Stream>> = lp.links.iter().map(|l| l.stream.clone()).collect();
        for s in &streams {
            unsafe { self.detach(lp, s) };
        }
        if !lp.core.is_null() {
            // SAFETY: on the loop thread; the registry and hook were made in connect.
            unsafe {
                if !lp.registry.is_null() {
                    pws::pw_proxy_destroy(lp.registry as *mut pws::pw_proxy);
                    lp.registry = ptr::null_mut();
                }
                spa::utils::hook::remove(lp.core_hook);
                lp.core_hook = std::mem::zeroed();
                pws::pw_core_disconnect(lp.core);
            }
            lp.core = ptr::null_mut();
        }
        lp.core_lost = false;
        self.connected.store(false, Relaxed);
    }

    unsafe fn attach(&self, lp: &mut LoopState, s: &Arc<Stream>, now: u64) {
        // SAFETY: on the loop thread. The link is boxed before its hook and events are handed to
        // PipeWire, and lp.links keeps it, and the stream the callbacks are given, alive until
        // detach has removed them.
        unsafe {
            let props = stream_properties(&s.target);
            let stream = pws::pw_stream_new(lp.core, c"Basedreader".as_ptr(), props);
            if stream.is_null() {
                s.set_retry_ns(now + RECONNECT_NS);
                return;
            }
            s.prepare_attach();
            s.set_pw(stream as *mut c_void);
            let mut link = Box::new(Link {
                stream: s.clone(),
                hook: std::mem::zeroed(),
                events: std::mem::zeroed(),
                node: ptr::null_mut(),
                node_hook: std::mem::zeroed(),
                node_events: std::mem::zeroed(),
            });
            link.events.version = pws::PW_VERSION_STREAM_EVENTS;
            link.events.state_changed = Some(on_stream_state);
            link.events.process = Some(on_process);
            pws::pw_stream_add_listener(
                stream,
                &mut link.hook,
                &link.events,
                Arc::as_ptr(s) as *mut c_void,
            );
            let pod = format_pod(&s.format);
            let mut params = [pod.as_ptr() as *const spas::spa_pod];
            let flags = pws::pw_stream_flags_PW_STREAM_FLAG_AUTOCONNECT
                | pws::pw_stream_flags_PW_STREAM_FLAG_MAP_BUFFERS
                | pws::pw_stream_flags_PW_STREAM_FLAG_RT_PROCESS;
            let r = pws::pw_stream_connect(
                stream,
                spas::SPA_DIRECTION_OUTPUT,
                pws::PW_ID_ANY,
                flags,
                params.as_mut_ptr(),
                1,
            );
            if r < 0 {
                spa::utils::hook::remove(link.hook);
                pws::pw_stream_destroy(stream);
                s.mark_detached();
                s.set_retry_ns(now + RECONNECT_NS);
                return;
            }
            s.mark_attached(now);
            lp.links.push(link);
        }
        self.attached_count.store(lp.links.len(), Relaxed);
    }

    unsafe fn detach(&self, lp: &mut LoopState, s: &Arc<Stream>) {
        let Some(i) = lp.links.iter().position(|l| Arc::ptr_eq(&l.stream, s)) else {
            return;
        };
        let mut link = lp.links.swap_remove(i);
        let stream = s.pw() as *mut pws::pw_stream;
        s.mark_detached();
        // SAFETY: on the loop thread. Removing the stream's hook takes the data loop's lock to
        // clear its real-time callbacks, so no callback runs for this stream once it returns.
        unsafe {
            if !link.node.is_null() {
                spa::utils::hook::remove(link.node_hook);
                pws::pw_proxy_destroy(link.node);
                link.node = ptr::null_mut();
            }
            spa::utils::hook::remove(link.hook);
            pws::pw_stream_disconnect(stream);
            pws::pw_stream_destroy(stream);
        }
        // A state change this detach caused, or one the connection's loss caused, needs nothing
        // more doing.
        s.take_broken();
        self.attached_count.store(lp.links.len(), Relaxed);
    }

    /// Keeps streams active until the keep-awake deadline, so that the device stays awake, then
    /// deactivates those with nothing to play; reactivates any that have something. Returns when
    /// it next needs to look.
    unsafe fn update_activity(&self, lp: &mut LoopState, now: u64) -> Option<u64> {
        let deadline = self.keep_awake_until.load(Acquire);
        let mut due = None::<u64>;
        let mut sooner = |t: u64| due = Some(due.map_or(t, |d: u64| d.min(t)));
        for link in &lp.links {
            let s = &link.stream;
            let stream = s.pw() as *mut pws::pw_stream;
            if s.is_active() {
                // A stream not yet scheduled since it connected costs nothing, and deactivating it
                // could race the session manager linking it.
                if s.process_calls() == 0 {
                    sooner(now + RECHECK_NS);
                    continue;
                }
                if now < deadline {
                    sooner(deadline);
                    continue;
                }
                if !s.is_empty() {
                    // Looked at again when what it holds will have played, unless it is paused.
                    sooner(now + s.pending_ns().unwrap_or(RECHECK_NS) + DRAIN_CHECK_NS);
                    continue;
                }
                if !s.is_drained() {
                    sooner(now + DRAIN_CHECK_NS);
                    continue;
                }
                if !s.deactivate_if_still_empty() {
                    sooner(now + RECHECK_NS);
                    continue;
                }
                // SAFETY: on the loop thread, with a live pw_stream.
                unsafe { pws::pw_stream_set_active(stream, false) };
            } else if !s.is_empty() {
                s.reactivate();
                // SAFETY: as above.
                unsafe { pws::pw_stream_set_active(stream, true) };
                let played = now + s.pending_ns().unwrap_or(RECHECK_NS) + DRAIN_CHECK_NS;
                sooner(deadline.max(played));
            }
        }
        due
    }

    unsafe fn arm_timer(&self, lp: &mut LoopState, at: Option<u64>, now: u64) {
        let mut value = spas::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        let mut interval = value;
        if let Some(at) = at {
            // An absolute time on CLOCK_MONOTONIC, the clock the loop's timers use. Never zero,
            // which would disarm it.
            let at = at.max(now + 1_000_000);
            value.tv_sec = (at / NS) as _;
            value.tv_nsec = (at % NS) as _;
        }
        // SAFETY: on the loop thread, with the timer added in start.
        unsafe {
            let utils = (*pws::pw_thread_loop_get_loop(self.thread_loop)).utils;
            spa_interface_call_method!(
                utils,
                spas::spa_loop_utils_methods,
                update_timer,
                lp.timer,
                &mut value,
                &mut interval,
                true
            );
        }
    }
}

/// Binds the stream's node, once the daemon has given it an id, to hear when it is running.
///
/// A pw_stream reaches its streaming state when the daemon starts its node, but the daemon puts the
/// node into the graph only after the stream has answered that start, and until then the sink is
/// not made to wait for it: a cycle handed over in between is mixed by nobody. The node's info
/// reports running once the node is in the graph, so the callback hands over nothing before that.
///
/// # Safety
/// On the loop thread, with a registry from the live connection or null.
unsafe fn watch_node(registry: *mut pws::pw_registry, link: &mut Link) {
    if !link.node.is_null() || registry.is_null() {
        return;
    }
    let stream = link.stream.pw() as *mut pws::pw_stream;
    // SAFETY: as documented; the hook and event table live in the boxed link, which outlives the
    // proxy, since detach destroys the proxy first.
    unsafe {
        let id = pws::pw_stream_get_node_id(stream);
        if id == pws::PW_ID_ANY {
            return;
        }
        let node = spa_interface_call_method!(
            registry,
            pws::pw_registry_methods,
            bind,
            id,
            pws::PW_TYPE_INTERFACE_Node.as_ptr() as *const c_char,
            pws::PW_VERSION_NODE,
            0
        ) as *mut pws::pw_proxy;
        if node.is_null() {
            return;
        }
        link.node = node;
        link.node_events.version = pws::PW_VERSION_NODE_EVENTS;
        link.node_events.info = Some(on_node_info);
        spa_interface_call_method!(
            node,
            pws::pw_node_methods,
            add_listener,
            &mut link.node_hook,
            &link.node_events,
            Arc::as_ptr(&link.stream) as *mut c_void
        );
    }
}

/// Moves pooled streams to the doomed list, oldest first, while the condition holds.
fn evict(shared: &mut Shared, mut condition: impl FnMut(&VecDeque<(Arc<Stream>, u64)>) -> bool) {
    while condition(&shared.pool) {
        let Some((s, _)) = shared.pool.pop_front() else {
            break;
        };
        shared.streams.retain(|a| !Arc::ptr_eq(a, &s));
        shared.doomed.push(s);
    }
}

unsafe fn set(props: *mut pws::pw_properties, key: &std::ffi::CStr, value: &std::ffi::CStr) {
    // SAFETY: the caller passes properties it owns.
    unsafe { pws::pw_properties_set(props, key.as_ptr(), value.as_ptr()) };
}

/// The properties of every stream. The names are Basedreader's own and no other program's, since
/// WirePlumber keys what it remembers about a stream by them. Nothing of Basedreader's is
/// remembered or restored, so that a volume or a target set for it in a mixer never outlives the
/// process and nothing stored from elsewhere is applied to it. node.latency asks for a quantum of
/// 512 where the graph's is free, and cannot force one: Basedreader never sets the quantum, the
/// rate, a volume or a mute, on its own node or any other.
unsafe fn stream_properties(target: &Option<CString>) -> *mut pws::pw_properties {
    // SAFETY: a new property list, handed to pw_stream_new.
    unsafe {
        let props = pws::pw_properties_new(ptr::null());
        for (key, value) in [
            (c"media.type", c"Audio"),
            (c"media.category", c"Playback"),
            (c"media.role", c"Accessibility"),
            (c"media.name", c"Basedreader"),
            (c"application.name", c"Basedreader"),
            (c"application.id", c"basedreader"),
            (c"node.name", c"basedreader"),
            (c"node.description", c"Basedreader"),
            (c"node.latency", c"512/48000"),
            (c"state.restore-props", c"false"),
            (c"state.restore-target", c"false"),
        ] {
            set(props, key, value);
        }
        if let Some(target) = target {
            set(props, c"target.object", target);
        }
        props
    }
}

fn format_pod(format: &Format) -> Vec<u8> {
    let mut info = spa::param::audio::AudioInfoRaw::new();
    info.set_format(spa::param::audio::AudioFormat::from_raw(
        format.sample.spa(),
    ));
    info.set_rate(format.rate);
    info.set_channels(format.channels);
    let mut position = [0u32; spa::param::audio::MAX_CHANNELS];
    let layout = format.positions();
    position[..layout.len()].copy_from_slice(layout);
    info.set_position(position);
    spa::pod::serialize::PodSerializer::serialize(
        std::io::Cursor::new(Vec::new()),
        &spa::pod::Value::Object(spa::pod::Object {
            type_: spas::SPA_TYPE_OBJECT_Format,
            id: spas::SPA_PARAM_EnumFormat,
            properties: info.into(),
        }),
    )
    .map(|(cursor, _)| cursor.into_inner())
    .unwrap_or_default()
}

unsafe extern "C" fn on_wake(data: *mut c_void, fd: c_int, _mask: u32) {
    let mut count: u64 = 0;
    // SAFETY: reads the eventfd's counter into a local, which also clears it.
    unsafe { libc::read(fd, &mut count as *mut u64 as *mut c_void, 8) };
    // SAFETY: data is the leaked engine, and this is the loop thread.
    unsafe { (*(data as *const Engine)).service() };
}

unsafe extern "C" fn on_timer(data: *mut c_void, _expirations: u64) {
    // SAFETY: as on_wake.
    unsafe { (*(data as *const Engine)).service() };
}

unsafe extern "C" fn on_core_error(
    data: *mut c_void,
    id: u32,
    _seq: c_int,
    res: c_int,
    _message: *const c_char,
) {
    if id == pws::PW_ID_CORE && res == -libc::EPIPE {
        // SAFETY: data is the leaked engine, and this is the loop thread. The core is not
        // destroyed from inside its own event; service does it on the next wake.
        let engine = unsafe { &*(data as *const Engine) };
        unsafe { (*engine.state.get()).core_lost = true };
        engine.signal();
    }
}

/// On the loop thread, sometimes from inside a call service makes, so it touches only the
/// stream's atomics and leaves the rest to the next service.
unsafe extern "C" fn on_stream_state(
    data: *mut c_void,
    _old: pws::pw_stream_state,
    state: pws::pw_stream_state,
    _error: *const c_char,
) {
    // SAFETY: data is the stream's address, which its link keeps alive.
    let s = unsafe { &*(data as *const Stream) };
    s.set_streaming(state == pws::pw_stream_state_PW_STREAM_STATE_STREAMING);
    if state == pws::pw_stream_state_PW_STREAM_STATE_ERROR
        || state == pws::pw_stream_state_PW_STREAM_STATE_UNCONNECTED
    {
        s.set_broken(true);
    }
    // The node has an id from its first state change on, which service binds to watch it.
    if let Some(Ok(engine)) = ENGINE.get() {
        engine.signal();
    }
}

unsafe extern "C" fn on_node_info(data: *mut c_void, info: *const pws::pw_node_info) {
    // SAFETY: data is the stream's address, which its link keeps alive, and info is PipeWire's
    // for the duration of the call.
    let (s, info) = unsafe { (&*(data as *const Stream), &*info) };
    if info.change_mask & pws::PW_NODE_CHANGE_MASK_STATE as u64 != 0 {
        s.set_running(info.state == pws::pw_node_state_PW_NODE_STATE_RUNNING);
    }
}

/// The real-time callback. It takes no lock, makes no system call, allocates nothing and never
/// waits: atomics, a copy from the ring, the gain stage, and PipeWire's own lock-free buffer and
/// time calls.
///
/// Whatever it hands over is a whole cycle's worth: when the ring holds less than the cycle
/// asks for, the rest is silence, and after the last frames it hands over one more cycle of
/// silence. PipeWire's converter keeps input that does not fill a cycle, and the resampler keeps
/// a few frames of history, until more input comes; without the silence, the end of every sound
/// would be held back and played at the start of the next one on the same stream.
unsafe extern "C" fn on_process(data: *mut c_void) {
    // SAFETY: data is the stream's address, which its link keeps alive while the callback can
    // run, and pw is its live pw_stream.
    unsafe {
        let s = &*(data as *const Stream);
        if !s.ready() {
            // Not yet mixed by anyone: the frames stay in the ring for the first cycle that is,
            // and the cycle is not counted as the graph consuming the stream.
            return;
        }
        let stream = s.pw() as *mut pws::pw_stream;
        let mut rp = s.begin_cycle();
        let available = s.available(rp);
        let mut took = false;
        if available > 0 || s.draining() {
            let b = pws::pw_stream_dequeue_buffer(stream);
            if !b.is_null() {
                let d = &mut *(*(*b).buffer).datas;
                let stride = s.stride();
                let mut take = 0u64;
                let mut filled = 0u64;
                if !d.data.is_null() {
                    let max = d.maxsize as u64 / stride as u64;
                    let requested = (*b).requested;
                    filled = if requested > 0 {
                        requested.min(max)
                    } else {
                        max
                    };
                    take = filled.min(available);
                    let dst =
                        std::slice::from_raw_parts_mut(d.data as *mut u8, filled as usize * stride);
                    let (sound, rest) = dst.split_at_mut(take as usize * stride);
                    s.copy_out(rp, sound, f32::from_bits(PROCESS_GAIN.load(Relaxed)));
                    rest.fill(s.silence_byte());
                }
                (*d.chunk).offset = 0;
                (*d.chunk).stride = stride as i32;
                (*d.chunk).size = (filled as usize * stride) as u32;
                // Counts only the frames from the ring, which come before the silence: it is
                // what pw_time.queued reports, and so what the position subtracts.
                (*b).size = take;
                pws::pw_stream_queue_buffer(stream, b);
                rp += take;
                DELIVERED.fetch_add(take, Relaxed);
                took = take > 0;
                s.note_handed(take, filled);
            }
        }
        s.end_cycle(rp);
        let mut t: pws::pw_time = std::mem::zeroed();
        pws::pw_stream_get_time_n(stream, &mut t, std::mem::size_of::<pws::pw_time>());
        let now = t.now.max(0) as u64;
        if took {
            // Frames handed over that are not yet heard: the graph's delay to the device, scaled
            // to the stream's rate, the buffers queued and what the resampler holds.
            let delay = if t.rate.denom > 0 {
                t.delay as f64 * t.rate.num as f64 * s.format.rate as f64 / t.rate.denom as f64
            } else {
                0.0
            };
            s.publish(now, rp, delay + t.queued as f64 + t.buffered as f64);
        }
        s.note_cycle(now, t.buffered);
    }
}
