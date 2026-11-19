//! linda-keyd: takes keys in the kernel for Linda. A system service, run as a system user of its
//! own with access to the input devices and to uinput, never as root.

use std::io;
use std::path::Path;

use keyd::core::Core;
use keyd::device::Devices;
use keyd::evdev::Evdev;
use keyd::protocol::SOCKET_PATH;
use keyd::server::{self, Notice, Notices, Server};
use keyd::session::{self, SystemBus};
use keyd::watchdog::{self, Notifier, Progress};

/// The exit status when the watchdog ends the helper.
const WATCHDOG_EXIT: i32 = 70;
/// The exit status after a panic on any thread.
const PANIC_EXIT: i32 = 101;

fn main() {
    // A thread that panicked would leave its part undone while the rest went on, the watchdog
    // among them; the whole helper ends instead, and the kernel lets go of what it held. No core
    // dump: it would hold keys typed.
    std::panic::set_hook(Box::new(|info| {
        eprintln!("linda-keyd: {info}");
        // SAFETY: ends the process at once, which is all that is wanted here.
        unsafe { libc::_exit(PANIC_EXIT) };
    }));
    if std::env::args().nth(1).as_deref() == Some("--version") {
        println!("linda-keyd {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    if let Err(e) = run() {
        eprintln!("linda-keyd: error: {e}");
        std::process::exit(1);
    }
}

fn run() -> io::Result<()> {
    // SAFETY: getuid cannot fail.
    if unsafe { libc::getuid() } == 0 {
        return Err(io::Error::other(
            "linda-keyd runs as a system user of its own, with access to the input devices, never as root",
        ));
    }
    let listener = server::listener(Path::new(SOCKET_PATH))?;
    let devices = Evdev::new();
    let guard = devices.guard();
    let notices = Notices::new()?;
    let progress = Progress::new();
    let n = notices.clone();
    session::watch_front(move |f| n.post(Notice::Front(f)))?;
    let n = notices.clone();
    session::watch_console(move |c| n.post(Notice::Console(c)))?;
    let mut bus = SystemBus::new();
    let admit = server::spawn_admitter(notices.clone(), move |peer| {
        session::admit(&mut bus, peer, &session::start_time)
    })?;
    watchdog::spawn(
        progress.clone(),
        guard,
        // SAFETY: ends the process at once, which is all that is wanted here: the kernel ends
        // the grabs and destroys the virtual keyboard, releasing every key it held down.
        Box::new(|| unsafe { libc::_exit(WATCHDOG_EXIT) }),
    )?;
    let mut server = Server::new(
        Core::new(devices),
        listener,
        notices,
        admit,
        progress,
        Notifier::from_env(),
    );
    eprintln!("linda-keyd: listening on {SOCKET_PATH}");
    server.run()
}
