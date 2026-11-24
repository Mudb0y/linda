//! The real device layer: keyboards read through their evdev nodes and grabbed with EVIOCGRAB, the
//! virtual keyboard made through uinput, and /dev/input watched with inotify for keyboards that
//! come and go. It runs only where the helper is installed as a service, and in the virtual
//! machines of the NixOS tests; never in a unit test, and never on a machine someone is typing on
//! while the helper is being developed.
//!
//! Everything about a device is checked against the open file itself before it is grabbed: the
//! node's device number leads back to the kernel's own record of the device in sysfs, so that a
//! node reused by another device between the scan and the open is noticed.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::mem::{ManuallyDrop, size_of};
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering::SeqCst};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use libc::{c_int, c_ulong};
use rustix::fs::inotify;
use rustix::ioctl::{Direction, Opcode, opcode};

use crate::device::{
    Devices, Found, GrabError, Guard, InputEvent, Keyboard, Lights, ReadError, VirtualKeyboard,
};
use crate::keys::{
    self, ABS_VOLUME, Assessment, CONSUMER_ABS, CONSUMER_BUTTONS, CONSUMER_RELS, Description,
    EV_ABS, EV_KEY, EV_LED, EV_MAX, EV_REL, EV_REP, EV_SYN, KEY_CNT, KEY_MAX, KeySet, LEDS,
    SYN_REPORT, VOLUME_MAX, is_virtual_key, rescale_volume,
};

const DEV_INPUT: &str = "/dev/input";
const UINPUT: &str = "/dev/uinput";
const SYS_CLASS_INPUT: &str = "/sys/class/input";
/// Where the kernel puts an input device with no parent, which is how a device made through
/// uinput appears.
const SYS_VIRTUAL_INPUT: &str = "/sys/devices/virtual/input";
const UDEV_DATA: &str = "/run/udev/data";

const VIRTUAL_NAME: &str = "Linda virtual keyboard";
const BUS_VIRTUAL: u16 = 0x06;
/// "Li" and "nd", so that the device can be recognised in a list of input devices.
const VIRTUAL_VENDOR: u16 = 0x4c69;
const VIRTUAL_PRODUCT: u16 = 0x6e64;

/// The most keyboards grabbed at once, the size of the table the watchdog thread reads.
const MAX_GRABBED: usize = 64;
/// The most reads of one device per turn of the loop, so that a device sending without pause
/// cannot keep the loop from the others, the socket and the timers.
const READS_PER_TURN: usize = 16;

const EVIOCGRAB: Opcode = opcode::write::<c_int>(b'E', 0x90);
const EVIOCREVOKE: Opcode = opcode::write::<c_int>(b'E', 0x91);
const UI_DEV_CREATE: Opcode = opcode::none(b'U', 1);
const UI_DEV_DESTROY: Opcode = opcode::none(b'U', 2);
const UI_DEV_SETUP: Opcode = opcode::write::<libc::uinput_setup>(b'U', 3);
const UI_ABS_SETUP: Opcode = opcode::write::<libc::uinput_abs_setup>(b'U', 4);
const UI_SET_EVBIT: Opcode = opcode::write::<c_int>(b'U', 100);
const UI_SET_KEYBIT: Opcode = opcode::write::<c_int>(b'U', 101);
const UI_SET_RELBIT: Opcode = opcode::write::<c_int>(b'U', 102);
const UI_SET_LEDBIT: Opcode = opcode::write::<c_int>(b'U', 105);

const fn read_op(group: u8, number: u8, len: usize) -> Opcode {
    opcode::from_components(Direction::Read, group, number, len)
}

fn eviocgname(len: usize) -> Opcode {
    read_op(b'E', 0x06, len)
}

fn eviocgkey(len: usize) -> Opcode {
    read_op(b'E', 0x18, len)
}

fn eviocgled(len: usize) -> Opcode {
    read_op(b'E', 0x19, len)
}

fn eviocgbit(ev: u16, len: usize) -> Opcode {
    read_op(b'E', 0x20 + ev as u8, len)
}

fn eviocgabs(abs: u16) -> Opcode {
    read_op(b'E', 0x40 + abs as u8, size_of::<libc::input_absinfo>())
}

fn ui_get_sysname(len: usize) -> Opcode {
    read_op(b'U', 44, len)
}

/// An ioctl that takes its argument as a value. The argument goes as a whole `unsigned long`, as
/// the kernel reads it: an `int` passed through the variadic call could leave the upper half of
/// the register undefined, which EVIOCGRAB would read as a request to grab.
fn ioctl_value(fd: RawFd, op: Opcode, value: c_ulong) -> io::Result<c_int> {
    // SAFETY: each request this is used with reads no memory, only the value.
    let r = unsafe { libc::ioctl(fd, op as _, value) };
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(r)
    }
}

/// An ioctl that fills `buf`, whose length the request encodes.
fn ioctl_read(fd: BorrowedFd<'_>, op: Opcode, buf: &mut [u8]) -> io::Result<c_int> {
    // SAFETY: the request writes at most the length it encodes, which is buf's.
    let r = unsafe { libc::ioctl(fd.as_raw_fd(), op as _, buf.as_mut_ptr()) };
    if r < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(r)
    }
}

/// The table of grabbed devices, and the virtual keyboard, that the watchdog thread reads. Lock-free,
/// so that a loop stopped anywhere, even between the two halves of a grab, cannot keep the watchdog
/// from it.
pub struct Grabs {
    slots: [AtomicI32; MAX_GRABBED],
    /// The virtual keyboard, while it exists.
    virt: AtomicI32,
    /// How many keys the virtual keyboard holds down: a key held there while no keyboard is grabbed
    /// repeats in the desktop as surely as one held while a keyboard is.
    virtual_keys: AtomicUsize,
    release: Release,
}

/// What the watchdog has done when it fires: one worker for each place in the table and one for the
/// virtual keyboard, started with the table and waiting, so that firing neither allocates nor starts
/// a thread, which a stalled loop holding the allocator's lock could prevent. Each acts on its own
/// place alone: a revoke waits for the device's lock in the kernel, which the loop holds while it is
/// in a call on that keyboard, and a loop stuck in one must keep the watchdog from that keyboard
/// alone, and from ending the helper, which closes them all, not at all.
struct Release {
    fired: Mutex<bool>,
    go: Condvar,
    /// How many workers have acted.
    done: AtomicUsize,
    /// Every worker was started; otherwise firing does all in turn on the watchdog thread.
    workers: AtomicBool,
}

impl Grabs {
    fn new() -> Arc<Grabs> {
        Grabs::with(revoke, destroy)
    }

    /// With what revoking a keyboard and destroying the virtual keyboard are, for the tests.
    fn with(revoke: fn(RawFd), destroy: fn(RawFd)) -> Arc<Grabs> {
        let g = Arc::new(Grabs {
            slots: std::array::from_fn(|_| AtomicI32::new(-1)),
            virt: AtomicI32::new(-1),
            virtual_keys: AtomicUsize::new(0),
            release: Release {
                fired: Mutex::new(false),
                go: Condvar::new(),
                done: AtomicUsize::new(0),
                workers: AtomicBool::new(true),
            },
        });
        for place in 0..=MAX_GRABBED {
            let me = g.clone();
            let started = thread::Builder::new()
                .name("release".into())
                .stack_size(64 * 1024)
                .spawn(move || {
                    let mut fired = me.release.fired.lock().unwrap_or_else(|e| e.into_inner());
                    while !*fired {
                        fired = me.release.go.wait(fired).unwrap_or_else(|e| e.into_inner());
                    }
                    drop(fired);
                    me.act(place, revoke, destroy);
                    me.release.done.fetch_add(1, SeqCst);
                });
            if started.is_err() {
                g.release.workers.store(false, SeqCst);
            }
        }
        g
    }

    fn act(&self, place: usize, revoke: fn(RawFd), destroy: fn(RawFd)) {
        match self.slots.get(place) {
            Some(s) => {
                let fd = s.load(SeqCst);
                if fd >= 0 {
                    revoke(fd);
                }
            }
            None => {
                let fd = self.virt.load(SeqCst);
                if fd >= 0 {
                    destroy(fd);
                }
            }
        }
    }

    /// Revokes every keyboard in the table and destroys the virtual keyboard, waiting for them at
    /// most `within`.
    fn release(&self, within: Duration) {
        if !self.release.workers.load(SeqCst) {
            for place in 0..=MAX_GRABBED {
                self.act(place, revoke, destroy);
            }
            return;
        }
        *self.release.fired.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.release.go.notify_all();
        let end = Instant::now() + within;
        while self.release.done.load(SeqCst) <= MAX_GRABBED && Instant::now() < end {
            thread::sleep(Duration::from_millis(1));
        }
    }

    fn set_virtual(&self, fd: RawFd) {
        self.virt.store(fd, SeqCst);
    }

    /// The kernel released whatever the virtual keyboard held as it went.
    fn virtual_destroyed(&self) {
        self.virtual_keys.store(0, SeqCst);
    }

    fn register(&self, fd: RawFd) -> Option<usize> {
        self.slots
            .iter()
            .position(|s| s.compare_exchange(-1, fd, SeqCst, SeqCst).is_ok())
    }

    fn unregister(&self, slot: usize) {
        self.slots[slot].store(-1, SeqCst);
    }
}

fn revoke(fd: RawFd) {
    let _ = ioctl_value(fd, EVIOCREVOKE, 0);
}

/// The kernel releases every key the virtual keyboard holds down as it destroys it, which it does
/// as the helper ends only once every thread has, a loop stuck in an uninterruptible call among
/// them: until then a key held down would go on repeating in the desktop.
fn destroy(fd: RawFd) {
    let _ = ioctl_value(fd, UI_DEV_DESTROY, 0);
}

/// The longest the watchdog waits for the keyboards to be revoked and the virtual keyboard
/// destroyed before it ends the helper anyway.
const REVOKE_WAIT: Duration = Duration::from_millis(100);

impl Guard for Grabs {
    fn holding(&self) -> bool {
        self.virtual_keys.load(SeqCst) > 0 || self.slots.iter().any(|s| s.load(SeqCst) >= 0)
    }

    /// EVIOCREVOKE ends the grab and makes the open file useless for good, so that a loop that
    /// comes back to life cannot go on with it. A device is registered before it is grabbed and
    /// unregistered only after it is let go and before it is closed, so every grab is in the table.
    fn release_all(&self) {
        self.release(REVOKE_WAIT);
    }
}

/// The keys the virtual keyboard holds down, as the count in [`Grabs`] has them. A press is counted
/// before it is written, so that none can be down uncounted, and a release only once it has gone
/// whole; a key whose release did not go counts until the device is destroyed.
#[derive(Default)]
struct VirtualDown(KeySet);

impl VirtualDown {
    fn writing(&mut self, grabs: &Grabs, code: u16, value: i32) {
        if value == 1 && !self.0.contains(code) {
            self.0.insert(code);
            grabs.virtual_keys.fetch_add(1, SeqCst);
        }
    }

    fn written(&mut self, grabs: &Grabs, code: u16, value: i32) {
        if value == 0 && self.0.contains(code) {
            self.0.remove(code);
            grabs.virtual_keys.fetch_sub(1, SeqCst);
        }
    }
}

pub struct Evdev {
    /// The kernel's number of the helper's own virtual keyboard, once made.
    own: Option<u32>,
    inotify: Option<OwnedFd>,
    grabs: Arc<Grabs>,
    /// /dev/uinput, opened as the helper starts and kept, on which every virtual keyboard is made;
    /// given up when a virtual keyboard cannot be destroyed, so that closing it destroys that one.
    uinput: Arc<Mutex<Option<File>>>,
}

impl Evdev {
    pub fn new() -> Evdev {
        Evdev {
            own: None,
            inotify: None,
            grabs: Grabs::new(),
            uinput: Arc::new(Mutex::new(None)),
        }
    }

    /// Takes /dev/uinput now and keeps it for the helper's life, every virtual keyboard being made
    /// on that one open file: uinput makes a new device on it after the last is destroyed. The unit
    /// has systemd open it as root and hand it over as standard input, so that the helper's user
    /// needs no access of its own, which udev and logind would take away whenever they pass over a
    /// node tagged for the user in front, as Steam's rules tag this one; started any other way, the
    /// helper opens it itself.
    pub fn keep_uinput(&mut self) -> io::Result<()> {
        let file = match handed_over()? {
            Some(f) => f,
            None => open_uinput()?,
        };
        *self.uinput.lock().unwrap_or_else(|e| e.into_inner()) = Some(file);
        Ok(())
    }
}

/// /dev/uinput, when it is standard input, moved to a descriptor of its own; standard input and
/// output, which systemd also made it, become /dev/null, so that nothing written or read by mistake
/// reaches the device.
fn handed_over() -> io::Result<Option<File>> {
    // SAFETY: descriptor 0 is open for the whole of the process's life.
    let stdin = unsafe { BorrowedFd::borrow_raw(0) };
    let Ok(st) = rustix::fs::fstat(stdin) else {
        return Ok(None);
    };
    let uinput = libc::makedev(UINPUT_MAJOR, UINPUT_MINOR);
    if st.st_mode & libc::S_IFMT != libc::S_IFCHR || st.st_rdev != uinput {
        return Ok(None);
    }
    let file = File::from(rustix::io::fcntl_dupfd_cloexec(stdin, 3)?);
    rustix::fs::fcntl_setfl(
        &file,
        rustix::fs::fcntl_getfl(&file)? | rustix::fs::OFlags::NONBLOCK,
    )?;
    let null = OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/null")?;
    for fd in [0, 1] {
        // SAFETY: both stay open, now on /dev/null.
        if unsafe { libc::dup2(null.as_raw_fd(), fd) } < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(Some(file))
}

/// The misc device uinput is, as the kernel numbers it (UINPUT_MINOR in linux/miscdevice.h).
const UINPUT_MAJOR: u32 = 10;
const UINPUT_MINOR: u32 = 223;

fn open_uinput() -> io::Result<File> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(UINPUT)
}

impl Default for Evdev {
    fn default() -> Self {
        Evdev::new()
    }
}

/// The input device a node's sysfs directory belongs to: inputN, where the event node is eventM.
fn input_dir(event_dir: &Path) -> io::Result<(PathBuf, u32)> {
    let real = fs::canonicalize(event_dir)?;
    let dir = real
        .parent()
        .ok_or_else(|| io::Error::other("an event node with no input device"))?
        .to_path_buf();
    let number = dir
        .file_name()
        .and_then(|n| n.to_str())
        .and_then(|n| n.strip_prefix("input"))
        .and_then(|n| n.parse().ok())
        .ok_or_else(|| io::Error::other(format!("{} is not an input device", dir.display())))?;
    Ok((dir, number))
}

fn is_parentless(input_dir: &Path) -> bool {
    input_dir.parent() == Some(Path::new(SYS_VIRTUAL_INPUT))
}

/// The udev property ID_SEAT of a character device, from udev's database; None when udev gives
/// none, which means seat0.
fn udev_seat(major: u32, minor: u32) -> Option<String> {
    let data = fs::read_to_string(format!("{UDEV_DATA}/c{major}:{minor}")).ok()?;
    data.lines()
        .find_map(|l| l.strip_prefix("E:ID_SEAT="))
        .map(str::to_owned)
}

fn parse_dev(text: &str) -> Option<(u32, u32)> {
    let (major, minor) = text.trim().split_once(':')?;
    Some((major.parse().ok()?, minor.parse().ok()?))
}

/// What sysfs says of an event node, without opening it. sysfs writes bitmaps in the kernel's
/// `unsigned long`, taken here to be the helper's own word; a 32-bit helper on a 64-bit kernel
/// would misread them, and take no keyboard.
fn describe_sysfs(event_dir: &Path, own: Option<u32>) -> io::Result<(u32, Description)> {
    let (dir, number) = input_dir(event_dir)?;
    let bits = usize::BITS as usize;
    let read = |name: &str| fs::read_to_string(dir.join(name));
    let caps = |name: &str| -> io::Result<KeySet> {
        KeySet::from_sysfs(&read(&format!("capabilities/{name}"))?, bits)
            .ok_or_else(|| io::Error::other(format!("unreadable capabilities/{name}")))
    };
    let types = caps("ev")?
        .iter()
        .filter(|t| *t <= EV_MAX)
        .fold(0u32, |a, t| a | 1 << t);
    let rel = bits64(&caps("rel")?);
    let abs = bits64(&caps("abs")?);
    let seat = parse_dev(&fs::read_to_string(event_dir.join("dev"))?)
        .and_then(|(major, minor)| udev_seat(major, minor));
    Ok((
        number,
        Description {
            name: read("name")?.trim_end().to_owned(),
            types,
            keys: caps("key")?,
            rel,
            abs,
            parentless: is_parentless(&dir),
            seat,
            ours: own == Some(number),
        },
    ))
}

/// What an open device says of itself.
fn describe_fd(
    file: &File,
    dir: &Path,
    seat: Option<String>,
    own: Option<u32>,
    number: u32,
) -> io::Result<Description> {
    let fd = file.as_fd();
    let mut types = [0u8; size_of::<c_ulong>()];
    ioctl_read(fd, eviocgbit(0, types.len()), &mut types)?;
    let types = KeySet::from_longs(&types)
        .iter()
        .filter(|t| *t <= EV_MAX)
        .fold(0u32, |a, t| a | 1 << t);
    let mut keys = [0u8; KEY_CNT / 8];
    ioctl_read(fd, eviocgbit(EV_KEY, keys.len()), &mut keys)?;
    // REL_CNT and ABS_CNT bits, sixteen and sixty-four, both within eight bytes.
    let mut rel = [0u8; 8];
    ioctl_read(fd, eviocgbit(EV_REL, rel.len()), &mut rel)?;
    let mut abs = [0u8; 8];
    ioctl_read(fd, eviocgbit(EV_ABS, abs.len()), &mut abs)?;
    let mut name = [0u8; 256];
    let len = ioctl_read(fd, eviocgname(name.len()), &mut name)?;
    let name = &name[..usize::try_from(len).unwrap_or(0).min(name.len())];
    let name = name.split(|b| *b == 0).next().unwrap_or_default();
    Ok(Description {
        name: String::from_utf8_lossy(name).into_owned(),
        types,
        keys: KeySet::from_longs(&keys),
        rel: bits64(&KeySet::from_longs(&rel)),
        abs: bits64(&KeySet::from_longs(&abs)),
        parentless: is_parentless(dir),
        seat,
        ours: own == Some(number),
    })
}

impl Devices for Evdev {
    type Keyboard = EvKeyboard;
    type Virtual = EvVirtual;

    fn scan(&mut self) -> io::Result<Vec<Found>> {
        let mut found = Vec::new();
        for entry in fs::read_dir(DEV_INPUT)? {
            let Ok(entry) = entry else { continue };
            let name = entry.file_name();
            let Some(name) = name.to_str().filter(|n| n.starts_with("event")) else {
                continue;
            };
            // A node that went or changed meanwhile is simply not there this time.
            let Ok((number, d)) = describe_sysfs(&Path::new(SYS_CLASS_INPUT).join(name), self.own)
            else {
                continue;
            };
            let assessment = keys::assess(&d);
            if assessment != Assessment::NotKeyboard {
                found.push(Found {
                    number,
                    node: format!("{DEV_INPUT}/{name}"),
                    name: d.name,
                    assessment,
                });
            }
        }
        Ok(found)
    }

    fn open(&mut self, found: &Found) -> io::Result<EvKeyboard> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&found.node)?;
        let rdev = file.metadata()?.rdev();
        let (major, minor) = (libc::major(rdev), libc::minor(rdev));
        let (dir, number) = input_dir(Path::new(&format!("/sys/dev/char/{major}:{minor}")))?;
        if number != found.number {
            return Err(io::Error::other(format!(
                "{} is now input device {number}, not {}",
                found.node, found.number
            )));
        }
        let d = describe_fd(&file, &dir, udev_seat(major, minor), self.own, number)?;
        if keys::assess(&d) != Assessment::Keyboard {
            return Err(io::Error::other(format!(
                "{} is not a keyboard to take",
                found.node
            )));
        }
        let mut leds = [0u8; size_of::<c_ulong>()];
        ioctl_read(file.as_fd(), eviocgbit(EV_LED, leds.len()), &mut leds)?;
        let leds = bits16(&leds);
        let volume = if d.abs & 1 << ABS_VOLUME != 0 {
            let mut info = [0u8; size_of::<libc::input_absinfo>()];
            ioctl_read(file.as_fd(), eviocgabs(ABS_VOLUME), &mut info)?;
            Some(abs_range(&info))
        } else {
            None
        };
        Ok(EvKeyboard {
            file: ManuallyDrop::new(file),
            grabs: self.grabs.clone(),
            slot: None,
            leds,
            volume,
        })
    }

    fn create_virtual(&mut self) -> io::Result<EvVirtual> {
        // A copy of the kept file is the same uinput device; destroying the virtual keyboard
        // through it leaves the file open for the next.
        let file = match &*self.uinput.lock().unwrap_or_else(|e| e.into_inner()) {
            Some(f) => f.try_clone()?,
            None => open_uinput()?,
        };
        let fd = file.as_raw_fd();
        for (op, code) in virtual_bits() {
            ioctl_value(fd, op, code.into())?;
        }
        for code in CONSUMER_ABS {
            let setup = virtual_axis(code);
            // SAFETY: the request reads one uinput_abs_setup, which this is.
            if unsafe {
                libc::ioctl(
                    fd,
                    UI_ABS_SETUP as _,
                    &setup as *const libc::uinput_abs_setup,
                )
            } < 0
            {
                return Err(io::Error::last_os_error());
            }
        }
        // SAFETY: uinput_setup is plain data, for which zero is a valid value.
        let mut setup: libc::uinput_setup = unsafe { std::mem::zeroed() };
        setup.id = libc::input_id {
            bustype: BUS_VIRTUAL,
            vendor: VIRTUAL_VENDOR,
            product: VIRTUAL_PRODUCT,
            version: 1,
        };
        for (to, from) in setup.name.iter_mut().zip(VIRTUAL_NAME.bytes()) {
            *to = from as libc::c_char;
        }
        // SAFETY: the request reads one uinput_setup, which this is.
        if unsafe { libc::ioctl(fd, UI_DEV_SETUP as _, &setup as *const libc::uinput_setup) } < 0 {
            return Err(io::Error::last_os_error());
        }
        ioctl_value(fd, UI_DEV_CREATE, 0)?;
        // From here the device exists, and dropping the file destroys it.
        self.grabs.set_virtual(file.as_raw_fd());
        let mut v = EvVirtual {
            file,
            grabs: self.grabs.clone(),
            kept: self.uinput.clone(),
            dir: PathBuf::new(),
            leds: Lights::default(),
            down: VirtualDown::default(),
        };
        let mut name = [0u8; 64];
        ioctl_read(v.file.as_fd(), ui_get_sysname(name.len()), &mut name)?;
        let name = name.split(|b| *b == 0).next().unwrap_or_default();
        let name = String::from_utf8_lossy(name).into_owned();
        let number = name
            .strip_prefix("input")
            .and_then(|n| n.parse().ok())
            .ok_or_else(|| io::Error::other(format!("uinput named its device {name:?}")))?;
        self.own = Some(number);
        v.dir = Path::new(SYS_VIRTUAL_INPUT).join(&name);
        Ok(v)
    }

    fn watch(&mut self, on: bool) -> io::Result<()> {
        if !on {
            self.inotify = None;
        } else if self.inotify.is_none() {
            let fd = inotify::init(inotify::CreateFlags::CLOEXEC | inotify::CreateFlags::NONBLOCK)?;
            // ATTRIB too: udev gives a new node its group and mode only after it appears, and the
            // helper can open it only then.
            inotify::add_watch(
                &fd,
                DEV_INPUT,
                inotify::WatchFlags::CREATE
                    | inotify::WatchFlags::ATTRIB
                    | inotify::WatchFlags::DELETE
                    | inotify::WatchFlags::MOVED_TO,
            )?;
            self.inotify = Some(fd);
        }
        Ok(())
    }

    fn watch_fd(&self) -> Option<BorrowedFd<'_>> {
        self.inotify.as_ref().map(|f| f.as_fd())
    }

    fn watch_drain(&mut self) -> bool {
        let Some(fd) = &self.inotify else {
            return false;
        };
        let mut buf = [0u8; 4096];
        let mut any = false;
        for _ in 0..READS_PER_TURN {
            match rustix::io::read(fd, &mut buf) {
                Ok(n) if n > 0 => any = true,
                _ => break,
            }
        }
        any
    }

    fn guard(&self) -> Arc<dyn Guard> {
        self.grabs.clone()
    }
}

/// What the virtual keyboard has, as the UI_SET_*BIT request for each code: its keys, the lights it
/// copies, repeats, and the consumer controls it passes on, whose absolute axes [`virtual_axis`]
/// sets up besides.
fn virtual_bits() -> Vec<(Opcode, u16)> {
    let mut bits: Vec<(Opcode, u16)> = [EV_KEY, EV_REL, EV_ABS, EV_LED, EV_REP]
        .into_iter()
        .map(|t| (UI_SET_EVBIT, t))
        .collect();
    bits.extend(
        (1..=KEY_MAX)
            .filter(|c| is_virtual_key(*c))
            .chain(CONSUMER_BUTTONS)
            .map(|c| (UI_SET_KEYBIT, c)),
    );
    bits.extend(CONSUMER_RELS.into_iter().map(|c| (UI_SET_RELBIT, c)));
    bits.extend(LEDS.into_iter().map(|l| (UI_SET_LEDBIT, l)));
    bits
}

/// The virtual keyboard's absolute axis `code`, 0 to VOLUME_MAX: ABS_VOLUME, whatever the range of
/// the keyboard it comes from (see [`keys::rescale_volume`]). It is left at its last value when
/// the helper lets go, a keyboard vanishes or events are lost, unlike a button, which is released:
/// a value to put it back to cannot be known, each keyboard having its own place of rest or none,
/// and nothing was found that reads the axis back from the virtual keyboard.
fn virtual_axis(code: u16) -> libc::uinput_abs_setup {
    // SAFETY: uinput_abs_setup is plain data, for which zero is a valid value.
    let mut setup: libc::uinput_abs_setup = unsafe { std::mem::zeroed() };
    setup.code = code;
    setup.absinfo.maximum = VOLUME_MAX;
    setup
}

/// The range in an input_absinfo, as EVIOCGABS fills it: the value, the minimum and the maximum
/// lead, each a 32-bit integer.
fn abs_range(info: &[u8]) -> (i32, i32) {
    let field = |i: usize| i32::from_ne_bytes([info[i], info[i + 1], info[i + 2], info[i + 3]]);
    (field(4), field(8))
}

/// The codes below 64 of a set, as bits.
fn bits64(set: &KeySet) -> u64 {
    set.iter().filter(|c| *c < 64).fold(0u64, |a, c| a | 1 << c)
}

fn bits16(longs: &[u8]) -> u16 {
    KeySet::from_longs(longs)
        .iter()
        .filter(|b| *b < 16)
        .fold(0u16, |a, b| a | 1 << b)
}

/// Events as the kernel lays them out: the time, whatever its size on this machine, then the
/// type, the code and the value in the last eight bytes.
fn parse_events(bytes: &[u8], out: &mut Vec<InputEvent>) {
    let size = size_of::<libc::input_event>();
    for e in bytes.chunks_exact(size) {
        let tail = &e[size - 8..];
        out.push(InputEvent {
            kind: u16::from_ne_bytes([tail[0], tail[1]]),
            code: u16::from_ne_bytes([tail[2], tail[3]]),
            value: i32::from_ne_bytes([tail[4], tail[5], tail[6], tail[7]]),
        });
    }
}

fn event_bytes(events: &[(u16, u16, i32)]) -> Vec<u8> {
    let size = size_of::<libc::input_event>();
    let mut b = vec![0u8; size * events.len()];
    for (chunk, (kind, code, value)) in b.chunks_exact_mut(size).zip(events) {
        let tail = &mut chunk[size - 8..];
        tail[..2].copy_from_slice(&kind.to_ne_bytes());
        tail[2..4].copy_from_slice(&code.to_ne_bytes());
        tail[4..].copy_from_slice(&value.to_ne_bytes());
    }
    b
}

/// Puts each ABS_VOLUME of a keyboard whose range is `volume` into the virtual keyboard's range.
/// One from a keyboard said to have none, which the kernel does not send, is made the bottom.
fn rescale(events: &mut [InputEvent], volume: Option<(i32, i32)>) {
    let (min, max) = volume.unwrap_or((0, 0));
    for e in events
        .iter_mut()
        .filter(|e| e.kind == EV_ABS && e.code == ABS_VOLUME)
    {
        e.value = rescale_volume(e.value, min, max);
    }
}

/// Reads whole events until there are none, or for READS_PER_TURN reads.
fn read_events(mut file: &File, out: &mut Vec<InputEvent>) -> Result<(), ReadError> {
    let mut buf = vec![0u8; size_of::<libc::input_event>() * 64];
    for _ in 0..READS_PER_TURN {
        match file.read(&mut buf) {
            Ok(0) => return Ok(()),
            Ok(n) => parse_events(&buf[..n], out),
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => return Ok(()),
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) if e.raw_os_error() == Some(libc::ENODEV) => return Err(ReadError::Gone),
            Err(e) => return Err(ReadError::Io(e)),
        }
    }
    Ok(())
}

pub struct EvKeyboard {
    /// Closed by hand as the keyboard is dropped, before its place in the grab table is given up.
    file: ManuallyDrop<File>,
    grabs: Arc<Grabs>,
    /// Its place in the grab table while grabbed.
    slot: Option<usize>,
    /// The lock lights it has.
    leds: u16,
    /// The range of its ABS_VOLUME, when it has one.
    volume: Option<(i32, i32)>,
}

impl Keyboard for EvKeyboard {
    fn fd(&self) -> BorrowedFd<'_> {
        self.file.as_fd()
    }

    fn read(&mut self, out: &mut Vec<InputEvent>) -> Result<(), ReadError> {
        let from = out.len();
        let read = read_events(&self.file, out);
        rescale(&mut out[from..], self.volume);
        read
    }

    fn keys_down(&self) -> io::Result<KeySet> {
        let mut keys = [0u8; KEY_CNT / 8];
        ioctl_read(self.file.as_fd(), eviocgkey(keys.len()), &mut keys)?;
        Ok(KeySet::from_longs(&keys))
    }

    fn grab(&mut self) -> Result<(), GrabError> {
        if self.slot.is_some() {
            return Ok(());
        }
        let fd = self.file.as_raw_fd();
        let slot = self
            .grabs
            .register(fd)
            .ok_or_else(|| GrabError::Io(io::Error::other("too many keyboards grabbed")))?;
        match ioctl_value(fd, EVIOCGRAB, 1) {
            Ok(_) => {
                self.slot = Some(slot);
                Ok(())
            }
            Err(e) => {
                self.grabs.unregister(slot);
                Err(match e.raw_os_error() {
                    Some(libc::EBUSY) => GrabError::Busy,
                    Some(libc::ENODEV) => GrabError::Gone,
                    _ => GrabError::Io(e),
                })
            }
        }
    }

    /// The keyboard keeps its place in the grab table, for the watchdog to revoke, until the
    /// kernel has let go.
    fn ungrab(&mut self) -> bool {
        let Some(slot) = self.slot else {
            return true;
        };
        if ioctl_value(self.file.as_raw_fd(), EVIOCGRAB, 0).is_err() {
            return false;
        }
        self.slot = None;
        self.grabs.unregister(slot);
        true
    }

    fn leds(&self) -> io::Result<u16> {
        let mut leds = [0u8; size_of::<c_ulong>()];
        ioctl_read(self.file.as_fd(), eviocgled(leds.len()), &mut leds)?;
        Ok(bits16(&leds))
    }

    /// Written through the file that holds the grab, which is the one way to the lights of a
    /// grabbed keyboard: the kernel ignores what other handlers send it.
    fn set_leds(&mut self, leds: u16) -> io::Result<()> {
        let mut events: Vec<(u16, u16, i32)> = LEDS
            .iter()
            .filter(|l| self.leds & 1 << **l != 0)
            .map(|l| (EV_LED, *l, i32::from(leds & 1 << *l != 0)))
            .collect();
        if events.is_empty() {
            return Ok(());
        }
        events.push((EV_SYN, SYN_REPORT, 0));
        (&*self.file).write_all(&event_bytes(&events))
    }
}

impl Drop for EvKeyboard {
    /// The grab is let go of explicitly, then the file is closed, which ends it whatever came of
    /// that, and only then is its place in the grab table given up.
    fn drop(&mut self) {
        self.ungrab();
        // SAFETY: the file is not used again.
        unsafe { ManuallyDrop::drop(&mut self.file) };
        if let Some(slot) = self.slot.take() {
            self.grabs.unregister(slot);
        }
    }
}

pub struct EvVirtual {
    file: File,
    /// Where the watchdog finds it.
    grabs: Arc<Grabs>,
    /// The kept /dev/uinput this is a copy of.
    kept: Arc<Mutex<Option<File>>>,
    /// Its directory in sysfs, inputN.
    dir: PathBuf,
    /// The lights as the kernel has them, from which it sends only changes.
    leds: Lights,
    down: VirtualDown,
}

impl VirtualKeyboard for EvVirtual {
    fn fd(&self) -> BorrowedFd<'_> {
        self.file.as_fd()
    }

    /// Appeared once its event node exists and udev has written its record, after which udev
    /// tells libinput, the X server and the compositors of it.
    fn appeared(&mut self) -> io::Result<bool> {
        let Ok(entries) = fs::read_dir(&self.dir) else {
            return Ok(false);
        };
        for entry in entries.flatten() {
            if !entry.file_name().to_string_lossy().starts_with("event") {
                continue;
            }
            let Ok(dev) = fs::read_to_string(entry.path().join("dev")) else {
                continue;
            };
            if let Some((major, minor)) = parse_dev(&dev) {
                return Ok(Path::new(&format!("{UDEV_DATA}/c{major}:{minor}")).exists());
            }
        }
        Ok(false)
    }

    fn emit(&mut self, code: u16, value: i32) -> io::Result<()> {
        let b = event_bytes(&[(EV_KEY, code, value), (EV_SYN, SYN_REPORT, 0)]);
        self.down.writing(&self.grabs, code, value);
        let n = (&self.file).write(&b)?;
        if n != b.len() {
            return Err(io::Error::other(format!(
                "uinput took {n} of {} bytes",
                b.len()
            )));
        }
        self.down.written(&self.grabs, code, value);
        Ok(())
    }

    fn emit_frame(&mut self, events: &[InputEvent]) -> io::Result<()> {
        let mut frame: Vec<(u16, u16, i32)> =
            events.iter().map(|e| (e.kind, e.code, e.value)).collect();
        frame.push((EV_SYN, SYN_REPORT, 0));
        let b = event_bytes(&frame);
        let buttons = || events.iter().filter(|e| e.kind == EV_KEY);
        for e in buttons() {
            self.down.writing(&self.grabs, e.code, e.value);
        }
        let n = (&self.file).write(&b)?;
        if n != b.len() {
            return Err(io::Error::other(format!(
                "uinput took {n} of {} bytes",
                b.len()
            )));
        }
        for e in buttons() {
            self.down.written(&self.grabs, e.code, e.value);
        }
        Ok(())
    }

    fn read_leds(&mut self) -> io::Result<Option<u16>> {
        self.take_leds()?;
        Ok(self.leds.read())
    }

    fn set_own_leds(&mut self, leds: u16) -> io::Result<()> {
        // What came before is read first, so that the echo awaited is of what this write changes.
        self.take_leds()?;
        let mut events: Vec<(u16, u16, i32)> = self
            .leds
            .own(leds, &LEDS)
            .into_iter()
            .map(|(code, value)| (EV_LED, code, value))
            .collect();
        if events.is_empty() {
            return Ok(());
        }
        events.push((EV_SYN, SYN_REPORT, 0));
        (&self.file).write_all(&event_bytes(&events))
    }
}

impl EvVirtual {
    /// Follows every light the kernel has handed the device.
    fn take_leds(&mut self) -> io::Result<()> {
        let mut events = Vec::new();
        match read_events(&self.file, &mut events) {
            Ok(()) => {}
            Err(ReadError::Gone) => return Err(io::Error::from_raw_os_error(libc::ENODEV)),
            Err(ReadError::Io(e)) => return Err(e),
        }
        for e in events.iter().filter(|e| e.kind == EV_LED) {
            self.leds.event(e.code, e.value != 0);
        }
        Ok(())
    }
}

impl Drop for EvVirtual {
    /// The kernel releases every key the device still holds down as it destroys it. When it will
    /// not destroy it on request, the kept file goes too, and closing the last copy destroys it.
    fn drop(&mut self) {
        self.grabs.set_virtual(-1);
        if ioctl_value(self.file.as_raw_fd(), UI_DEV_DESTROY, 0).is_err() {
            log!("the virtual keyboard would not be destroyed, so /dev/uinput is closed");
            drop(self.kept.lock().unwrap_or_else(|e| e.into_inner()).take());
        }
        self.grabs.virtual_destroyed();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The request numbers against the kernel's headers, on the architectures where the
    /// encoding is the common one.
    #[test]
    #[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
    fn the_requests_are_the_kernels() {
        assert_eq!(EVIOCGRAB, 0x4004_4590);
        assert_eq!(EVIOCREVOKE, 0x4004_4591);
        assert_eq!(eviocgkey(96), 0x8060_4518);
        assert_eq!(eviocgbit(EV_KEY, 96), 0x8060_4521);
        assert_eq!(eviocgbit(0, 8), 0x8008_4520);
        assert_eq!(eviocgled(8), 0x8008_4519);
        assert_eq!(eviocgname(256), 0x8100_4506);
        assert_eq!(UI_DEV_CREATE, 0x5501);
        assert_eq!(UI_DEV_DESTROY, 0x5502);
        assert_eq!(UI_DEV_SETUP, 0x405c_5503);
        assert_eq!(UI_SET_EVBIT, 0x4004_5564);
        assert_eq!(UI_SET_KEYBIT, 0x4004_5565);
        assert_eq!(UI_SET_LEDBIT, 0x4004_5569);
        assert_eq!(ui_get_sysname(64), 0x8040_552c);
        assert_eq!(UI_SET_RELBIT, 0x4004_5566);
        assert_eq!(UI_ABS_SETUP, 0x401c_5504);
        assert_eq!(eviocgabs(ABS_VOLUME), 0x8018_4560);
        assert_eq!(eviocgbit(EV_REL, 8), 0x8008_4522);
        assert_eq!(eviocgbit(EV_ABS, 8), 0x8008_4523);
    }

    #[test]
    fn the_virtual_keyboard_has_the_consumer_controls_it_passes_on() {
        let bits = virtual_bits();
        let has = |op: Opcode, code: u16| bits.contains(&(op, code));
        for t in [EV_KEY, EV_REL, EV_ABS, EV_LED, EV_REP] {
            assert!(has(UI_SET_EVBIT, t), "{t}");
        }
        assert!(has(UI_SET_KEYBIT, keys::KEY_A) && has(UI_SET_KEYBIT, KEY_MAX));
        for code in CONSUMER_BUTTONS {
            assert!(has(UI_SET_KEYBIT, code), "{code:#x}");
        }
        for code in CONSUMER_RELS {
            assert!(has(UI_SET_RELBIT, code), "{code:#x}");
        }
        // Nothing that would make it a pointer, a joystick or a tablet pad: no other axis, and of
        // the buttons only those of consumer controls.
        assert_eq!(
            bits.iter().filter(|(op, _)| *op == UI_SET_RELBIT).count(),
            CONSUMER_RELS.len()
        );
        for (op, code) in &bits {
            if *op == UI_SET_KEYBIT {
                assert!(
                    is_virtual_key(*code) || CONSUMER_BUTTONS.contains(code),
                    "{code:#x}"
                );
            }
        }
        let volume = virtual_axis(ABS_VOLUME);
        assert_eq!(
            (volume.code, volume.absinfo.minimum, volume.absinfo.maximum),
            (ABS_VOLUME, 0, VOLUME_MAX)
        );
        assert_eq!(CONSUMER_ABS, [ABS_VOLUME]);
    }

    #[test]
    fn volume_is_read_from_the_keyboards_range() {
        // An input_absinfo of value 7, minimum -10, maximum 4085.
        let mut info = Vec::new();
        for v in [7i32, -10, 4085, 0, 0, 0] {
            info.extend_from_slice(&v.to_ne_bytes());
        }
        assert_eq!(info.len(), size_of::<libc::input_absinfo>());
        assert_eq!(abs_range(&info), (-10, 4085));
        let ev = |kind, code, value| InputEvent { kind, code, value };
        let mut events = [
            ev(EV_ABS, ABS_VOLUME, 4085),
            ev(EV_ABS, ABS_VOLUME, -10),
            ev(EV_KEY, ABS_VOLUME, 4085),
            ev(EV_REL, keys::REL_HWHEEL, -1),
        ];
        rescale(&mut events, Some((-10, 4085)));
        assert_eq!(
            events.map(|e| e.value),
            [VOLUME_MAX, 0, 4085, -1],
            "only ABS_VOLUME is rescaled"
        );
        let mut stray = [ev(EV_ABS, ABS_VOLUME, 99)];
        rescale(&mut stray, None);
        assert_eq!(stray[0].value, 0);
        let s: KeySet = [keys::REL_HWHEEL, keys::REL_HWHEEL_HI_RES, 64, 200]
            .into_iter()
            .collect();
        assert_eq!(bits64(&s), 0x1040);
    }

    #[test]
    fn events_go_out_and_come_back_whole() {
        let b = event_bytes(&[(EV_KEY, 30, 1), (EV_SYN, SYN_REPORT, 0), (EV_KEY, 30, 2)]);
        assert_eq!(b.len(), 3 * size_of::<libc::input_event>());
        let mut out = Vec::new();
        parse_events(&b, &mut out);
        assert_eq!(
            out,
            vec![
                InputEvent {
                    kind: EV_KEY,
                    code: 30,
                    value: 1
                },
                InputEvent {
                    kind: EV_SYN,
                    code: SYN_REPORT,
                    value: 0
                },
                InputEvent {
                    kind: EV_KEY,
                    code: 30,
                    value: 2
                },
            ]
        );
        // The same layout libc gives the structure.
        // SAFETY: input_event is plain data, for which zero is a valid value.
        let mut e: libc::input_event = unsafe { std::mem::zeroed() };
        e.type_ = EV_KEY;
        e.code = 58;
        e.value = -1;
        // SAFETY: reading the bytes of a plain structure.
        let raw = unsafe {
            std::slice::from_raw_parts(
                (&e as *const libc::input_event).cast::<u8>(),
                size_of::<libc::input_event>(),
            )
        };
        let mut out = Vec::new();
        parse_events(raw, &mut out);
        assert_eq!(
            out,
            vec![InputEvent {
                kind: EV_KEY,
                code: 58,
                value: -1
            }]
        );
    }

    #[test]
    fn device_numbers_and_seats_are_read_as_sysfs_and_udev_write_them() {
        assert_eq!(parse_dev("13:64\n"), Some((13, 64)));
        assert_eq!(parse_dev("13"), None);
        assert!(is_parentless(Path::new(
            "/sys/devices/virtual/input/input31"
        )));
        assert!(!is_parentless(Path::new(
            "/sys/devices/virtual/misc/uhid/0005:046D:B342.0001/input/input25"
        )));
        assert!(!is_parentless(Path::new(
            "/sys/devices/platform/i8042/serio0/input/input3"
        )));
        assert_eq!(
            bits16(&{
                let mut b = [0u8; size_of::<c_ulong>()];
                b[0] = 0b110;
                b
            }),
            if cfg!(target_endian = "little") {
                0b110
            } else {
                0
            }
        );
    }

    #[test]
    fn the_grab_table_is_what_the_watchdog_sees() {
        let g = Grabs::new();
        assert!(!g.holding());
        let a = g.register(1000).unwrap();
        let b = g.register(1001).unwrap();
        assert_ne!(a, b);
        assert!(g.holding());
        g.unregister(a);
        assert!(g.holding());
        g.unregister(b);
        assert!(!g.holding());
        for i in 0..MAX_GRABBED {
            assert!(g.register(2000 + i as RawFd).is_some());
        }
        assert!(g.register(9999).is_none());
    }

    static REVOKED: std::sync::Mutex<Vec<RawFd>> = std::sync::Mutex::new(Vec::new());

    #[test]
    fn a_keyboard_whose_revoke_hangs_keeps_the_watchdog_from_no_other() {
        fn revoke(fd: RawFd) {
            if fd == 3000 {
                thread::sleep(Duration::from_secs(10));
            }
            REVOKED.lock().unwrap().push(fd);
        }
        fn destroy(fd: RawFd) {
            REVOKED.lock().unwrap().push(-fd);
        }
        let g = Grabs::with(revoke, destroy);
        for fd in [3000, 3001, 3002] {
            g.register(fd).unwrap();
        }
        g.set_virtual(4000);
        let started = Instant::now();
        g.release(REVOKE_WAIT);
        let took = started.elapsed();
        assert!(took < REVOKE_WAIT + Duration::from_millis(400), "{took:?}");
        let mut revoked = REVOKED.lock().unwrap().clone();
        revoked.sort();
        assert_eq!(revoked, vec![-4000, 3001, 3002]);
    }

    static DESTROYED: std::sync::Mutex<Vec<RawFd>> = std::sync::Mutex::new(Vec::new());

    #[test]
    fn a_key_down_on_the_virtual_keyboard_holds_the_watchdog_with_nothing_grabbed() {
        use crate::watchdog::{self, Progress};
        fn revoke(_fd: RawFd) {}
        fn destroy(fd: RawFd) {
            DESTROYED.lock().unwrap().push(fd);
        }
        let g = Grabs::with(revoke, destroy);
        g.set_virtual(5000);
        let mut down = VirtualDown::default();
        assert!(!g.holding());
        // As the virtual keyboard writes a press of Shift, a second press of it, a press of A and
        // Shift's release.
        down.writing(&g, 42, 1);
        assert!(g.holding());
        down.written(&g, 42, 1);
        down.writing(&g, 42, 1);
        down.written(&g, 42, 1);
        down.writing(&g, 30, 1);
        down.written(&g, 30, 1);
        down.writing(&g, 42, 0);
        assert!(g.holding(), "counted up before the release went");
        down.written(&g, 42, 0);
        assert!(g.holding(), "A is still down");
        down.writing(&g, 30, 0);
        down.written(&g, 30, 0);
        assert!(!g.holding());
        // A release that never went counts the key down still.
        down.writing(&g, 42, 1);
        down.written(&g, 42, 1);
        down.writing(&g, 42, 0);
        assert!(g.holding());
        let progress = Progress::new();
        let stopped = Instant::now();
        progress.turned(stopped, None);
        let ended = Arc::new(AtomicBool::new(false));
        let e = ended.clone();
        watchdog::spawn(progress, g.clone(), Box::new(move || e.store(true, SeqCst)))
            .unwrap()
            .join()
            .unwrap();
        assert!(ended.load(SeqCst));
        assert!(stopped.elapsed() > crate::protocol::STALL_LIMIT);
        assert_eq!(*DESTROYED.lock().unwrap(), vec![5000]);
        g.virtual_destroyed();
        assert!(!g.holding());
    }
}
