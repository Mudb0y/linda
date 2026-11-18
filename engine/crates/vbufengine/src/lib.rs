//! The virtual buffer's renderer: walks a web document over a peer-to-peer connection to its
//! application, writes it as the render stream libnvdaVbuf builds NVDA's buffer storage from, and
//! then watches it, writing each change as an update to that buffer. render.rs describes the stream.
//!
//! It is loaded into NVDA's process and called through ctypes, which releases the GIL; nothing here
//! touches Python. All the work happens on one thread of the engine's own, which the first render
//! starts; the functions below hand it requests and take what it produced.

#[allow(dead_code)]
#[rustfmt::skip]
mod tables;

mod chromium;
mod engine;
mod named;
mod render;
mod walk;

use std::collections::VecDeque;
use std::ffi::{CStr, CString, c_char};
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Condvar, LazyLock, Mutex, mpsc};
use std::task::{Context, Poll};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use futures_util::task::AtomicWaker;

/// Batches nobody has taken are dropped beyond this many, oldest first: without a reader they only
/// grow.
const MAX_QUEUED_BATCHES: usize = 4096;
const RENDER_FLAGS_KNOWN: u32 = 3;
/// The engine's thread recurses once for each level of a document's nesting, down to
/// render::MAX_DEPTH: under a kilobyte a level in a release build, over eight in a debug one. An
/// overflow takes NVDA's whole process down, and only the pages touched are ever committed.
pub(crate) const ENGINE_STACK: usize = 32 << 20;

pub(crate) enum Request {
    Render {
        bus: String,
        doc: String,
        handle: i32,
        flags: u32,
        reply: mpsc::Sender<RenderReply>,
    },
    Unwatch {
        bus: String,
        doc: String,
    },
    Shutdown,
}

pub(crate) struct RenderReply {
    pub stream: Option<Vec<u16>>,
    pub info: String,
}

/// Requests into the engine's thread, which waits for them among its other work.
pub(crate) struct Inbox {
    queue: Mutex<VecDeque<Request>>,
    waker: AtomicWaker,
}

impl Inbox {
    fn send(&self, r: Request) {
        self.queue.lock().unwrap().push_back(r);
        self.waker.wake();
    }

    pub(crate) fn poll_recv(&self, cx: &mut Context<'_>) -> Poll<Request> {
        self.waker.register(cx.waker());
        match self.queue.lock().unwrap().pop_front() {
            Some(r) => Poll::Ready(r),
            None => Poll::Pending,
        }
    }
}

struct Batch {
    stream: Option<Vec<u16>>,
    info: String,
}

const NOT_STARTED: u8 = 0;
const RUNNING: u8 = 1;
const SHUT_DOWN: u8 = 2;

/// Batches out of the engine's thread, for bre_vbuf_next.
pub(crate) struct Shared {
    batches: Mutex<VecDeque<Batch>>,
    ready: Condvar,
    state: AtomicU8,
}

impl Shared {
    pub(crate) fn push(&self, stream: Option<Vec<u16>>, info: String) {
        let mut q = self.batches.lock().unwrap();
        q.push_back(Batch { stream, info });
        while q.len() > MAX_QUEUED_BATCHES {
            q.pop_front();
        }
        self.ready.notify_all();
    }
}

static SHARED: LazyLock<Arc<Shared>> = LazyLock::new(|| {
    Arc::new(Shared {
        batches: Mutex::new(VecDeque::new()),
        ready: Condvar::new(),
        state: AtomicU8::new(NOT_STARTED),
    })
});

struct Engine {
    inbox: Arc<Inbox>,
    thread: JoinHandle<()>,
}

static ENGINE: Mutex<Option<Engine>> = Mutex::new(None);

fn inbox() -> Arc<Inbox> {
    let mut e = ENGINE.lock().unwrap();
    if let Some(engine) = e.as_ref() {
        return engine.inbox.clone();
    }
    let inbox = Arc::new(Inbox {
        queue: Mutex::new(VecDeque::new()),
        waker: AtomicWaker::new(),
    });
    let shared = SHARED.clone();
    shared.state.store(RUNNING, Ordering::SeqCst);
    let thread_inbox = inbox.clone();
    let thread = std::thread::Builder::new()
        .name("vbuf engine".into())
        .stack_size(ENGINE_STACK)
        .spawn(move || zbus::block_on(engine::run(thread_inbox, shared)))
        .expect("the engine's thread starts");
    *e = Some(Engine {
        inbox: inbox.clone(),
        thread,
    });
    inbox
}

fn string_arg(p: *const c_char) -> String {
    if p.is_null() {
        return String::new();
    }
    unsafe { CStr::from_ptr(p) }.to_string_lossy().into_owned()
}

unsafe fn give_stream(stream: Option<Vec<u16>>, out: *mut *mut u16, length: *mut usize) {
    let (p, n) = match stream {
        Some(v) => {
            let b = v.into_boxed_slice();
            let n = b.len();
            (Box::into_raw(b) as *mut u16, n)
        }
        None => (std::ptr::null_mut(), 0),
    };
    unsafe {
        *out = p;
        *length = n;
    }
}

unsafe fn give_str(s: String, out: *mut *mut c_char) {
    unsafe { *out = CString::new(s).unwrap_or_default().into_raw() };
}

/// Renders the document at doc_path in the application bus_name, and from then on watches it.
/// Blocks; never call it on NVDA's main thread. Returns 1 with the stream in *stream and *length (in
/// UTF-16 units, freed with bre_vbuf_free_stream), or 0 with *stream null. Either way *info is a
/// JSON object freed with bre_vbuf_free_str: complete, busy, docURL, objects, seconds and timings,
/// or error.
///
/// flags: 1 returns the top of a large document first, with complete false, and the rest as an
/// update batch whose events hold "complete"; 2 lets updates keep unchanged children with R.
///
/// # Safety
/// bus_name and doc_path are NUL-terminated; stream, length and info are writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_render(
    bus_name: *const c_char,
    doc_path: *const c_char,
    doc_handle: i32,
    flags: u32,
    stream: *mut *mut u16,
    length: *mut usize,
    info: *mut *mut c_char,
) -> i32 {
    let (tx, rx) = mpsc::channel();
    inbox().send(Request::Render {
        bus: string_arg(bus_name),
        doc: string_arg(doc_path),
        handle: doc_handle,
        flags: flags & RENDER_FLAGS_KNOWN,
        reply: tx,
    });
    let reply = rx.recv().unwrap_or(RenderReply {
        stream: None,
        info: r#"{"error": "the engine shut down"}"#.into(),
    });
    let ok = reply.stream.is_some();
    unsafe {
        give_stream(reply.stream, stream, length);
        give_str(reply.info, info);
    }
    ok as i32
}

/// The next batch of changes to any watched document. Returns 1 with a batch, 0 when none came
/// within timeout_ms, -1 once the engine has shut down (after waiting the timeout, so that a loop
/// around it does not spin). *stream is null for a batch that carries events only; *info is a JSON
/// object, freed with bre_vbuf_free_str: bus, doc, events, docURL. Events are drawn from
/// "loadComplete", "reload", "gone", "complete" and "urlChanged".
///
/// # Safety
/// stream, length and info are writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_next(
    timeout_ms: u32,
    stream: *mut *mut u16,
    length: *mut usize,
    info: *mut *mut c_char,
) -> i32 {
    let deadline = Instant::now() + Duration::from_millis(timeout_ms.into());
    let shared = SHARED.clone();
    let shut_at_entry = shared.state.load(Ordering::SeqCst) == SHUT_DOWN;
    let mut q = shared.batches.lock().unwrap();
    loop {
        if !shut_at_entry {
            if let Some(b) = q.pop_front() {
                unsafe {
                    give_stream(b.stream, stream, length);
                    give_str(b.info, info);
                }
                return 1;
            }
            if shared.state.load(Ordering::SeqCst) == SHUT_DOWN {
                break;
            }
        }
        let now = Instant::now();
        if now >= deadline {
            if shut_at_entry {
                break;
            }
            unsafe {
                *stream = std::ptr::null_mut();
                *length = 0;
                *info = std::ptr::null_mut();
            }
            return 0;
        }
        q = shared.ready.wait_timeout(q, deadline - now).unwrap().0;
    }
    unsafe {
        *stream = std::ptr::null_mut();
        *length = 0;
        *info = std::ptr::null_mut();
    }
    -1
}

/// Stops watching the document; nothing more comes for it.
///
/// # Safety
/// bus_name and doc_path are NUL-terminated.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_unwatch(bus_name: *const c_char, doc_path: *const c_char) {
    let e = ENGINE.lock().unwrap();
    if let Some(engine) = e.as_ref() {
        engine.inbox.send(Request::Unwatch {
            bus: string_arg(bus_name),
            doc: string_arg(doc_path),
        });
    }
}

/// Stops the engine's thread and closes its connections. bre_vbuf_next then returns -1; a later
/// render starts it again.
#[unsafe(no_mangle)]
pub extern "C" fn bre_vbuf_shutdown() {
    let engine = ENGINE.lock().unwrap().take();
    let shared = SHARED.clone();
    if let Some(engine) = engine {
        engine.inbox.send(Request::Shutdown);
        let _ = engine.thread.join();
    }
    shared.state.store(SHUT_DOWN, Ordering::SeqCst);
    shared.batches.lock().unwrap().clear();
    shared.ready.notify_all();
}

/// # Safety
/// stream and length are what bre_vbuf_render or bre_vbuf_next gave.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_free_stream(stream: *mut u16, length: usize) {
    if !stream.is_null() {
        drop(unsafe { Box::from_raw(std::ptr::slice_from_raw_parts_mut(stream, length)) });
    }
}

/// # Safety
/// s is what bre_vbuf_render or bre_vbuf_next stored in *info.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_free_str(s: *mut c_char) {
    if !s.is_null() {
        drop(unsafe { CString::from_raw(s) });
    }
}

/// The ID the buffer gives the object at path, whose last part is not a number, as a render
/// gives it; see named.rs.
///
/// # Safety
/// path is NUL-terminated.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn bre_vbuf_named_id(path: *const c_char) -> i32 {
    named::id(&string_arg(path))
}

/// The path of the object a render or bre_vbuf_named_id gave id, freed with bre_vbuf_free_str, or
/// null when none was.
#[unsafe(no_mangle)]
pub extern "C" fn bre_vbuf_named_path(id: i32) -> *mut c_char {
    named::path(id)
        .and_then(|p| CString::new(p).ok())
        .map_or(std::ptr::null_mut(), CString::into_raw)
}
