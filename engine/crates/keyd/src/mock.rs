//! A mocked device layer: keyboards and a virtual keyboard that exist only in memory, which a test
//! types on, unplugs, makes fail, and inspects. Each device has a socket pair of its own, so that
//! the helper's real loop can poll it as it would a device node.

use std::collections::{BTreeMap, VecDeque};
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, BorrowedFd};
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Duration;

use crate::device::{
    Devices, Found, GrabError, Guard, InputEvent, Keyboard, ReadError, VirtualKeyboard,
};
use crate::keys::{self, Assessment, Description, EV_KEY, EV_SYN, KeySet, SYN_DROPPED, SYN_REPORT};

struct Wake {
    ours: UnixStream,
    theirs: UnixStream,
}

impl Wake {
    fn new() -> Wake {
        let (ours, theirs) = UnixStream::pair().expect("a socket pair");
        ours.set_nonblocking(true).unwrap();
        theirs.set_nonblocking(true).unwrap();
        Wake { ours, theirs }
    }

    fn poke(&self) {
        let _ = (&self.theirs).write(&[1]);
    }

    fn drain(&self) {
        let mut b = [0u8; 64];
        while matches!((&self.ours).read(&mut b), Ok(n) if n > 0) {}
    }
}

struct Dev {
    description: Description,
    /// Keys physically down.
    down: KeySet,
    queue: VecDeque<InputEvent>,
    open: usize,
    grabbed: bool,
    /// Another program holds it.
    busy: bool,
    gone: bool,
    leds: u16,
    /// A key that goes down at the moment the helper grabs.
    down_at_grab: Option<u16>,
    /// Letting go fails, as when the call is interrupted.
    ungrab_fails: bool,
    grabs: usize,
    wake: Arc<Wake>,
}

#[derive(Default)]
struct Virt {
    exists: bool,
    made: usize,
    /// Every key event the virtual keyboard put out, the kernel's own releases at its destruction
    /// included.
    emitted: Vec<(u16, i32)>,
    down: KeySet,
    /// Destroyed by the guard, from outside the loop, which still has it: it takes no more keys.
    destroyed: bool,
    from_desktop: VecDeque<u16>,
    wake: Option<Arc<Wake>>,
}

#[derive(Default)]
struct State {
    devices: BTreeMap<u32, Dev>,
    next_number: u32,
    virt: Virt,
    watching: bool,
    changed: bool,
    fail_create: bool,
    never_appears: bool,
    fail_emit: bool,
    /// One key event the virtual keyboard refuses once, as uinput does when its write is
    /// interrupted, and takes the next.
    fail_emit_once: Option<(u16, i32)>,
    /// How long the next read of any keyboard hangs, standing in for a helper that stops.
    hang_reads: Option<Duration>,
    released_by_guard: bool,
    watch_wake: Option<Arc<Wake>>,
}

/// The test's hold on the mock.
#[derive(Clone, Default)]
pub struct World(Arc<Mutex<State>>);

impl World {
    pub fn new() -> World {
        let w = World::default();
        w.lock().next_number = 10;
        w.lock().watch_wake = Some(Arc::new(Wake::new()));
        w
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn devices(&self) -> MockDevices {
        let watch = self.lock().watch_wake.clone().expect("made by new");
        MockDevices(self.clone(), watch)
    }

    /// Plugs in a full keyboard.
    pub fn plug(&self, name: &str) -> u32 {
        self.plug_device(keys::tests::keyboard(name))
    }

    pub fn plug_device(&self, description: Description) -> u32 {
        let mut s = self.lock();
        let n = s.next_number;
        s.next_number += 1;
        s.devices.insert(
            n,
            Dev {
                description,
                down: KeySet::new(),
                queue: VecDeque::new(),
                open: 0,
                grabbed: false,
                busy: false,
                gone: false,
                leds: 0,
                down_at_grab: None,
                ungrab_fails: false,
                grabs: 0,
                wake: Arc::new(Wake::new()),
            },
        );
        s.changed = true;
        if s.watching
            && let Some(w) = &s.watch_wake
        {
            w.poke();
        }
        n
    }

    fn input(&self, n: u32, code: u16, value: i32) {
        let mut s = self.lock();
        let d = s.devices.get_mut(&n).expect("a plugged device");
        match value {
            1 => d.down.insert(code),
            0 => d.down.remove(code),
            _ => {}
        }
        if d.open > 0 && !d.gone {
            d.queue.push_back(InputEvent {
                kind: EV_KEY,
                code,
                value,
            });
            d.queue.push_back(InputEvent {
                kind: EV_SYN,
                code: SYN_REPORT,
                value: 0,
            });
            d.wake.poke();
        }
    }

    pub fn press(&self, n: u32, code: u16) {
        self.input(n, code, 1);
    }

    pub fn release(&self, n: u32, code: u16) {
        self.input(n, code, 0);
    }

    pub fn repeat(&self, n: u32, code: u16) {
        self.input(n, code, 2);
    }

    /// The kernel's buffer overflowed: what was in it is lost, and the key changes made meanwhile
    /// are known only from the state.
    pub fn overflow(&self, n: u32, released_unseen: &[u16]) {
        let mut s = self.lock();
        let d = s.devices.get_mut(&n).unwrap();
        for c in released_unseen {
            d.down.remove(*c);
        }
        d.queue.clear();
        d.queue.push_back(InputEvent {
            kind: EV_SYN,
            code: SYN_DROPPED,
            value: 0,
        });
        d.queue.push_back(InputEvent {
            kind: EV_SYN,
            code: SYN_REPORT,
            value: 0,
        });
        d.wake.poke();
    }

    pub fn unplug(&self, n: u32) {
        let mut s = self.lock();
        let d = s.devices.get_mut(&n).unwrap();
        d.gone = true;
        d.grabbed = false;
        d.wake.poke();
        s.changed = true;
        if s.watching
            && let Some(w) = &s.watch_wake
        {
            w.poke();
        }
    }

    /// A key goes down between the helper's look at the keys and its grab taking effect.
    pub fn press_at_grab(&self, n: u32, code: u16) {
        self.lock().devices.get_mut(&n).unwrap().down_at_grab = Some(code);
    }

    pub fn grabs(&self, n: u32) -> usize {
        self.lock().devices[&n].grabs
    }

    pub fn set_busy(&self, n: u32, busy: bool) {
        self.lock().devices.get_mut(&n).unwrap().busy = busy;
    }

    /// A key goes down, or up, after the helper's last read and before it asks which keys are down:
    /// the kernel's state has the change, and the event is thrown away as it is asked.
    pub fn change_unread(&self, n: u32, code: u16, down: bool) {
        let mut s = self.lock();
        let d = s.devices.get_mut(&n).unwrap();
        if down {
            d.down.insert(code);
        } else {
            d.down.remove(code);
        }
    }

    /// Whether letting go of the keyboard fails, as the kernel's does when it is interrupted.
    pub fn fail_ungrab(&self, n: u32, fails: bool) {
        self.lock().devices.get_mut(&n).unwrap().ungrab_fails = fails;
    }

    pub fn grabbed(&self, n: u32) -> bool {
        self.lock().devices[&n].grabbed
    }

    pub fn is_open(&self, n: u32) -> bool {
        self.lock().devices[&n].open > 0
    }

    pub fn any_grabbed(&self) -> bool {
        self.lock().devices.values().any(|d| d.grabbed)
    }

    pub fn leds(&self, n: u32) -> u16 {
        self.lock().devices[&n].leds
    }

    pub fn set_device_leds(&self, n: u32, leds: u16) {
        self.lock().devices.get_mut(&n).unwrap().leds = leds;
    }

    pub fn fail_create(&self, fail: bool) {
        self.lock().fail_create = fail;
    }

    pub fn never_appears(&self, never: bool) {
        self.lock().never_appears = never;
    }

    pub fn fail_emit(&self, fail: bool) {
        self.lock().fail_emit = fail;
    }

    pub fn fail_emit_once(&self, code: u16, value: i32) {
        self.lock().fail_emit_once = Some((code, value));
    }

    pub fn hang_next_read(&self, d: Duration) {
        self.lock().hang_reads = Some(d);
    }

    pub fn released_by_guard(&self) -> bool {
        self.lock().released_by_guard
    }

    pub fn virtual_exists(&self) -> bool {
        self.lock().virt.exists
    }

    pub fn virtual_made(&self) -> usize {
        self.lock().virt.made
    }

    pub fn emitted(&self) -> Vec<(u16, i32)> {
        self.lock().virt.emitted.clone()
    }

    /// Keys held down on the virtual keyboard.
    pub fn virtual_down(&self) -> KeySet {
        self.lock().virt.down.clone()
    }

    /// The X server or the console sets the lock lights on the virtual keyboard.
    pub fn desktop_sets_leds(&self, leds: u16) {
        let mut s = self.lock();
        s.virt.from_desktop.push_back(leds);
        if let Some(w) = &s.virt.wake {
            w.poke();
        }
    }
}

pub struct MockDevices(World, Arc<Wake>);

pub struct MockKeyboard {
    world: World,
    n: u32,
    wake: Arc<Wake>,
}

pub struct MockVirtual {
    world: World,
    wake: Arc<Wake>,
}

impl Devices for MockDevices {
    type Keyboard = MockKeyboard;
    type Virtual = MockVirtual;

    fn scan(&mut self) -> io::Result<Vec<Found>> {
        let mut s = self.0.lock();
        s.changed = false;
        Ok(s.devices
            .iter()
            .filter(|(_, d)| !d.gone)
            .filter_map(|(n, d)| {
                let assessment = keys::assess(&d.description);
                (assessment != Assessment::NotKeyboard).then(|| Found {
                    number: *n,
                    node: format!("/dev/input/event{n}"),
                    name: d.description.name.clone(),
                    assessment,
                })
            })
            .collect())
    }

    fn open(&mut self, found: &Found) -> io::Result<MockKeyboard> {
        let mut s = self.0.lock();
        let d = s
            .devices
            .get_mut(&found.number)
            .filter(|d| !d.gone)
            .ok_or_else(|| io::Error::from_raw_os_error(libc::ENOENT))?;
        d.open += 1;
        Ok(MockKeyboard {
            world: self.0.clone(),
            n: found.number,
            wake: d.wake.clone(),
        })
    }

    fn create_virtual(&mut self) -> io::Result<MockVirtual> {
        let mut s = self.0.lock();
        if s.fail_create {
            return Err(io::Error::from_raw_os_error(libc::EACCES));
        }
        assert!(!s.virt.exists, "two virtual keyboards at once");
        let wake = Arc::new(Wake::new());
        s.virt.exists = true;
        s.virt.destroyed = false;
        s.virt.made += 1;
        s.virt.wake = Some(wake.clone());
        Ok(MockVirtual {
            world: self.0.clone(),
            wake,
        })
    }

    fn watch(&mut self, on: bool) -> io::Result<()> {
        self.0.lock().watching = on;
        Ok(())
    }

    fn watch_fd(&self) -> Option<BorrowedFd<'_>> {
        Some(self.1.ours.as_fd())
    }

    fn watch_drain(&mut self) -> bool {
        let mut s = self.0.lock();
        if let Some(w) = &s.watch_wake {
            w.drain();
        }
        std::mem::take(&mut s.changed)
    }

    fn guard(&self) -> Arc<dyn Guard> {
        Arc::new(MockGuard(self.0.clone()))
    }
}

impl Keyboard for MockKeyboard {
    fn fd(&self) -> BorrowedFd<'_> {
        self.wake.ours.as_fd()
    }

    fn read(&mut self, out: &mut Vec<InputEvent>) -> Result<(), ReadError> {
        self.wake.drain();
        let hang = self.world.lock().hang_reads.take();
        if let Some(d) = hang {
            std::thread::sleep(d);
        }
        let mut s = self.world.lock();
        let d = s.devices.get_mut(&self.n).unwrap();
        if d.gone {
            return Err(ReadError::Gone);
        }
        out.extend(d.queue.drain(..));
        Ok(())
    }

    /// As the kernel's EVIOCGKEY, which also throws away every key event queued for this open file
    /// and not yet read (__evdev_flush_queue in drivers/input/evdev.c).
    fn keys_down(&self) -> io::Result<KeySet> {
        let mut s = self.world.lock();
        let d = s.devices.get_mut(&self.n).unwrap();
        if d.gone {
            return Err(io::Error::from_raw_os_error(libc::ENODEV));
        }
        d.queue.retain(|e| e.kind != EV_KEY);
        Ok(d.down.clone())
    }

    fn grab(&mut self) -> Result<(), GrabError> {
        let mut s = self.world.lock();
        let d = s.devices.get_mut(&self.n).unwrap();
        if d.gone {
            Err(GrabError::Gone)
        } else if d.busy {
            Err(GrabError::Busy)
        } else {
            d.grabbed = true;
            d.grabs += 1;
            if let Some(code) = d.down_at_grab.take() {
                d.down.insert(code);
                d.queue.push_back(InputEvent {
                    kind: EV_KEY,
                    code,
                    value: 1,
                });
            }
            Ok(())
        }
    }

    fn ungrab(&mut self) -> bool {
        let mut s = self.world.lock();
        let d = s.devices.get_mut(&self.n).unwrap();
        if d.ungrab_fails {
            return false;
        }
        d.grabbed = false;
        true
    }

    fn leds(&self) -> io::Result<u16> {
        Ok(self.world.lock().devices[&self.n].leds)
    }

    fn set_leds(&mut self, leds: u16) -> io::Result<()> {
        self.world.lock().devices.get_mut(&self.n).unwrap().leds = leds;
        Ok(())
    }
}

impl Drop for MockKeyboard {
    fn drop(&mut self) {
        let mut s = self.world.lock();
        let d = s.devices.get_mut(&self.n).unwrap();
        d.open -= 1;
        d.grabbed = false;
        d.queue.clear();
    }
}

impl VirtualKeyboard for MockVirtual {
    fn fd(&self) -> BorrowedFd<'_> {
        self.wake.ours.as_fd()
    }

    fn appeared(&mut self) -> io::Result<bool> {
        Ok(!self.world.lock().never_appears)
    }

    fn emit(&mut self, code: u16, value: i32) -> io::Result<()> {
        let mut s = self.world.lock();
        if s.fail_emit || s.virt.destroyed {
            return Err(io::Error::from_raw_os_error(libc::ENODEV));
        }
        if s.fail_emit_once == Some((code, value)) {
            s.fail_emit_once = None;
            return Err(io::Error::from_raw_os_error(libc::EINTR));
        }
        s.virt.emitted.push((code, value));
        match value {
            1 => s.virt.down.insert(code),
            0 => s.virt.down.remove(code),
            _ => {}
        }
        Ok(())
    }

    fn read_leds(&mut self) -> io::Result<Option<u16>> {
        self.wake.drain();
        Ok(self.world.lock().virt.from_desktop.drain(..).next_back())
    }
}

impl Drop for MockVirtual {
    /// As the kernel does on destroying an input device: a release for every key still down.
    fn drop(&mut self) {
        let mut s = self.world.lock();
        let held: Vec<u16> = s.virt.down.iter().collect();
        for code in held {
            s.virt.emitted.push((code, 0));
        }
        s.virt.down = KeySet::new();
        s.virt.exists = false;
        s.virt.wake = None;
    }
}

struct MockGuard(World);

impl Guard for MockGuard {
    fn holding(&self) -> bool {
        self.0.any_grabbed() || !self.0.lock().virt.down.is_empty()
    }

    fn release_all(&self) {
        let mut s = self.0.lock();
        for d in s.devices.values_mut() {
            d.grabbed = false;
        }
        if s.virt.exists {
            let held: Vec<u16> = s.virt.down.iter().collect();
            for code in held {
                s.virt.emitted.push((code, 0));
            }
            s.virt.down = KeySet::new();
            s.virt.destroyed = true;
        }
        s.released_by_guard = true;
    }
}
