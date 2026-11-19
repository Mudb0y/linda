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

fn main() {
    // With _exit rather than an abort, so that no core dump holds keys typed.
    keyd::end_on_panic();
    if std::env::args().nth(1).as_deref() == Some("--version") {
        println!("linda-keyd {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    if let Err(e) = run() {
        keyd::log_line(format_args!("error: {e}"));
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
    let mut devices = Evdev::new();
    if let Err(e) = devices.keep_uinput() {
        // Tried again whenever a virtual keyboard is wanted; nothing is grabbed without one.
        keyd::log_line(format_args!("cannot open /dev/uinput: {e}"));
    }
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
    keyd::log_line(format_args!("listening on {SOCKET_PATH}"));
    server.run()
}
