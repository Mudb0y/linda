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
use std::mem::size_of;
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI32, Ordering::SeqCst};
use std::sync::{Arc, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use libc::{c_int, c_ulong};
use rustix::fs::inotify;
use rustix::ioctl::{Direction, Opcode, opcode};

use crate::device::{
    Devices, Found, GrabError, Guard, InputEvent, Keyboard, ReadError, VirtualKeyboard,
};
use crate::keys::{
    self, Assessment, Description, EV_KEY, EV_LED, EV_MAX, EV_REP, EV_SYN, KEY_CNT, KEY_MAX,
    KeySet, LEDS, SYN_REPORT, is_virtual_key,
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
const UI_SET_EVBIT: Opcode = opcode::write::<c_int>(b'U', 100);
const UI_SET_KEYBIT: Opcode = opcode::write::<c_int>(b'U', 101);
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

/// The table of grabbed devices that the watchdog thread reads. Lock-free, so that a loop stopped
/// anywhere, even between the two halves of a grab, cannot keep the watchdog from it.
pub struct Grabs {
    slots: [AtomicI32; MAX_GRABBED],
}

impl Grabs {
    fn new() -> Grabs {
        Grabs {
            slots: std::array::from_fn(|_| AtomicI32::new(-1)),
        }
    }

    fn register(&self, fd: RawFd) -> Option<usize> {
        self.slots
            .iter()
            .position(|s| s.compare_exchange(-1, fd, SeqCst, SeqCst).is_ok())
    }

    fn unregister(&self, slot: usize) {
        self.slots[slot].store(-1, SeqCst);
    }

    /// Revokes every device in the table, each on a thread of its own, and waits for them at most
    /// `within`. A revoke waits for the device's lock in the kernel, which the loop holds while it
    /// is in a call on that keyboard; a loop stuck in one must keep the watchdog from that keyboard
    /// alone, and from ending the helper, which closes them all, not at all.
    fn release_with(&self, revoke: fn(RawFd), within: Duration) {
        let (done, finished) = mpsc::channel();
        let mut started = 0;
        for s in &self.slots {
            let fd = s.load(SeqCst);
            if fd < 0 {
                continue;
            }
            let done = done.clone();
            let spawned = thread::Builder::new().name("revoke".into()).spawn(move || {
                revoke(fd);
                let _ = done.send(());
            });
            match spawned {
                Ok(_) => started += 1,
                Err(_) => revoke(fd),
            }
        }
        let end = Instant::now() + within;
        for _ in 0..started {
            if finished
                .recv_timeout(end.saturating_duration_since(Instant::now()))
                .is_err()
            {
                return;
            }
        }
    }
}

fn revoke(fd: RawFd) {
    let _ = ioctl_value(fd, EVIOCREVOKE, 0);
}

/// The longest the watchdog waits for the keyboards to be revoked before it ends the helper anyway.
const REVOKE_WAIT: Duration = Duration::from_millis(100);

impl Guard for Grabs {
    fn holding(&self) -> bool {
        self.slots.iter().any(|s| s.load(SeqCst) >= 0)
    }

    /// EVIOCREVOKE ends the grab and makes the open file useless for good, so that a loop that
    /// comes back to life cannot go on with it. A device is registered before it is grabbed and
    /// unregistered only after it is let go and before it is closed, so every grab is in the table.
    fn release_all(&self) {
        self.release_with(revoke, REVOKE_WAIT);
    }
}

pub struct Evdev {
    /// The kernel's number of the helper's own virtual keyboard, once made.
    own: Option<u32>,
    inotify: Option<OwnedFd>,
    grabs: Arc<Grabs>,
    /// /dev/uinput, opened as the helper starts and kept, on which every virtual keyboard is made.
    uinput: Option<File>,
}

impl Evdev {
    pub fn new() -> Evdev {
        Evdev {
            own: None,
            inotify: None,
            grabs: Arc::new(Grabs::new()),
            uinput: None,
        }
    }

    /// Opens /dev/uinput now and keeps it for the helper's life. The helper's access to it is an
    /// entry in the node's access list, which systemd sets as the service starts; but udev and
    /// logind take every other user's entry off a node tagged for the user in front, as Steam's rules
    /// tag /dev/uinput, at each pass over it, which comes whenever a session comes to the front. So
    /// the node is opened while the entry is sure to be there, and every virtual keyboard is made on
    /// that one open file: uinput makes a new device on it after the last is destroyed.
    pub fn keep_uinput(&mut self) -> io::Result<()> {
        self.uinput = Some(open_uinput()?);
        Ok(())
    }
}

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
    let seat = parse_dev(&fs::read_to_string(event_dir.join("dev"))?)
        .and_then(|(major, minor)| udev_seat(major, minor));
    Ok((
        number,
        Description {
            name: read("name")?.trim_end().to_owned(),
            types,
            keys: caps("key")?,
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
    let mut name = [0u8; 256];
    let len = ioctl_read(fd, eviocgname(name.len()), &mut name)?;
    let name = &name[..usize::try_from(len).unwrap_or(0).min(name.len())];
    let name = name.split(|b| *b == 0).next().unwrap_or_default();
    Ok(Description {
        name: String::from_utf8_lossy(name).into_owned(),
        types,
        keys: KeySet::from_longs(&keys),
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
        Ok(EvKeyboard {
            file,
            grabs: self.grabs.clone(),
            slot: None,
            leds,
        })
    }

    fn create_virtual(&mut self) -> io::Result<EvVirtual> {
        // A copy of the kept file is the same uinput device; destroying the virtual keyboard
        // through it leaves the file open for the next.
        let file = match &self.uinput {
            Some(f) => f.try_clone()?,
            None => open_uinput()?,
        };
        let fd = file.as_raw_fd();
        for t in [EV_KEY, EV_LED, EV_REP] {
            ioctl_value(fd, UI_SET_EVBIT, t.into())?;
        }
        for code in (1..=KEY_MAX).filter(|c| is_virtual_key(*c)) {
            ioctl_value(fd, UI_SET_KEYBIT, code.into())?;
        }
        for led in LEDS {
            ioctl_value(fd, UI_SET_LEDBIT, led.into())?;
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
        let mut v = EvVirtual {
            file,
            dir: PathBuf::new(),
            leds: 0,
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
    file: File,
    grabs: Arc<Grabs>,
    /// Its place in the grab table while grabbed.
    slot: Option<usize>,
    /// The lock lights it has.
    leds: u16,
}

impl Keyboard for EvKeyboard {
    fn fd(&self) -> BorrowedFd<'_> {
        self.file.as_fd()
    }

    fn read(&mut self, out: &mut Vec<InputEvent>) -> Result<(), ReadError> {
        read_events(&self.file, out)
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

    fn ungrab(&mut self) {
        if let Some(slot) = self.slot.take() {
            let _ = ioctl_value(self.file.as_raw_fd(), EVIOCGRAB, 0);
            self.grabs.unregister(slot);
        }
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
        (&self.file).write_all(&event_bytes(&events))
    }
}

impl Drop for EvKeyboard {
    /// The grab is let go of explicitly, then the file is closed, which would end it anyway.
    fn drop(&mut self) {
        self.ungrab();
    }
}

pub struct EvVirtual {
    file: File,
    /// Its directory in sysfs, inputN.
    dir: PathBuf,
    /// The lights as the desktop set them, from which the kernel sends only changes.
    leds: u16,
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
        let n = (&self.file).write(&b)?;
        if n != b.len() {
            return Err(io::Error::other(format!(
                "uinput took {n} of {} bytes",
                b.len()
            )));
        }
        Ok(())
    }

    fn read_leds(&mut self) -> io::Result<Option<u16>> {
        let mut events = Vec::new();
        match read_events(&self.file, &mut events) {
            Ok(()) => {}
            Err(ReadError::Gone) => return Err(io::Error::from_raw_os_error(libc::ENODEV)),
            Err(ReadError::Io(e)) => return Err(e),
        }
        let mut changed = false;
        for e in events.iter().filter(|e| e.kind == EV_LED && e.code < 16) {
            if e.value != 0 {
                self.leds |= 1 << e.code;
            } else {
                self.leds &= !(1 << e.code);
            }
            changed = true;
        }
        Ok(changed.then_some(self.leds))
    }
}

impl Drop for EvVirtual {
    /// The kernel releases every key the device still holds down as it destroys it.
    fn drop(&mut self) {
        let _ = ioctl_value(self.file.as_raw_fd(), UI_DEV_DESTROY, 0);
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
        let g = Grabs::new();
        for fd in [3000, 3001, 3002] {
            g.register(fd).unwrap();
        }
        fn revoke(fd: RawFd) {
            if fd == 3000 {
                thread::sleep(Duration::from_secs(10));
            }
            REVOKED.lock().unwrap().push(fd);
        }
        let started = Instant::now();
        g.release_with(revoke, REVOKE_WAIT);
        let took = started.elapsed();
        assert!(took < REVOKE_WAIT + Duration::from_millis(400), "{took:?}");
        let mut revoked = REVOKED.lock().unwrap().clone();
        revoked.sort();
        assert_eq!(revoked, vec![3001, 3002]);
    }
}
