//! The protocol between linda-keyd and the one client it serves, Linda's kernel key source. This
//! module is the whole of it: a client can be written from what is here without reading the rest
//! of the helper.
//!
//! # The socket
//!
//! The helper listens on [`SOCKET_PATH`], a Unix socket of type `SOCK_SEQPACKET`. Each message is
//! one packet: the kernel keeps the boundaries, so there is no length prefix, and a message is
//! never split or run together with another. In Python that is
//! `socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)`, `connect`, `send` of one message and
//! `recv(256)` of one message.
//!
//! The first byte of a message is its type. The client's types are below 0x80, the helper's 0x80
//! and above. Integers are little-endian and fields are packed with no padding: the Python
//! `struct` format given with each message is its exact layout, and a message whose length is not
//! that format's size is malformed. A message that ends in text has its fixed part first and UTF-8
//! text in the rest of the packet. No empty packet is ever sent, so reading zero bytes means that
//! the other side has closed the connection.
//!
//! # The conversation
//!
//! 1. The client connects and sends HELLO within [`HELLO_TIMEOUT`].
//! 2. The helper checks who is calling: the peer's credentials on the socket, then logind, which
//!    must place the process in the session that is in front on seat0 (or, for a process outside
//!    any session, the user's graphical session, which must be that one), then polkit, which must
//!    allow the process the action [`POLKIT_ACTION`]. It answers WELCOME, or GOODBYE and closes.
//!    Only one client is admitted at a time: another of the session in front is turned away with
//!    GOODBYE BUSY, and one of a session that is not in front gives way to a newcomer of the
//!    session that is, with GOODBYE REPLACED.
//! 3. From WELCOME on, the client sends something at least every [`HEARTBEAT_INTERVAL`], a
//!    HEARTBEAT when it has nothing else to say. The helper answers each HEARTBEAT with a HEARTBEAT
//!    carrying the same counter, which lets the client see that the helper is alive too. After
//!    [`HEARTBEAT_TIMEOUT`] of silence the helper lets go of everything and sends GOODBYE SILENT.
//! 4. START asks the helper to capture. The first time, it makes its virtual keyboard and waits
//!    for the desktop to take it up, which takes about a second. It then opens every keyboard of
//!    the seat and grabs each one as soon as none of its keys is down, telling the client about
//!    each with DEVICE. LOCKS says which lock lights are on as soon as the helper knows, and again
//!    whenever they change.
//! 5. While a keyboard is held, every press, release and repeat read from it comes to the client
//!    as KEY, in the order read, at once. A press asks for a verdict: the client answers VERDICT,
//!    keep or pass. A release and a repeat ask nothing: a key whose press was kept has its repeats
//!    and its release kept, and one whose press was passed has them passed. The helper puts a press
//!    on its virtual keyboard only once every press read before it has been decided, so the client
//!    may be sent several presses before it has answered the first, and answers them in any order.
//!    A release waits behind the presses read before it, in the same way, if its key is one of the
//!    modifiers (both Shifts, Controls, Alts and Supers, and Caps Lock), or if its own press, or an
//!    earlier press of the same key, is still waiting ahead of it; any other release goes out as
//!    soon as its press has gone out, ahead of presses still waiting, so that a key let go of is
//!    not held down long enough for the desktop to repeat it. A release overtaking another key's press changes
//!    nothing for a program.
//!    A press not answered within [`VERDICT_TIMEOUT`] of its reading is passed on, and EXPIRED
//!    says so, once every press read before it has been decided: at its bound, or later, when a
//!    press before it holds the line longer (with WAIT). After a freeze of the whole helper that it
//!    forgives, as across a suspend, the bound of every press still waiting runs afresh from the
//!    thaw, the client having been frozen too, so EXPIRED may come well after the press's reading
//!    plus its bound by the client's clock. Until EXPIRED, a verdict for it counts, after its
//!    bound too; one that comes after EXPIRED is ignored. When the client fails to answer a run
//!    of presses spanning [`UNANSWERED_SPAN`], the helper lets go and sends GOODBYE UNANSWERED;
//!    what makes a run is given with that bound.
//! 6. STOP asks the helper to let go, which it confirms with RELEASED STOPPED.
//! 7. RELEASED, for whatever reason, means the helper holds no keyboard any more and has released
//!    every key it held down on its virtual keyboard; the client should take every key it was told
//!    is down as up. After BACKGROUND and SESSION_UNKNOWN the helper captures again by itself once
//!    the client's session is in front again; after any other reason only a new START does.
//! 8. GOODBYE comes before the helper closes the connection, with the reason. Whenever the
//!    connection ends, for any reason, the helper has let go of everything.
//!
//! Whenever the helper lets go, with RELEASED or as the connection ends, the presses still waiting
//! for a verdict are passed on as if their bounds had come, each put on the virtual keyboard and
//! released again with the rest, so that the application has had them; a verdict that comes for
//! one later is ignored. Only after the emergency chord are those dropped instead: neither the
//! virtual keyboard nor the application has them. Keys read and not yet dealt with when the
//! helper lets go (the rest of what it read with the chord's last key), and what each keyboard has
//! for it as it is let go of, go on to the application as taps, pressed and released at once,
//! whatever the reason for letting go, the emergency chord included. They come too late for the
//! client to be asked, and each is sent as a KEY press and a KEY release without flags, in the
//! order they go out; a key the client was told is down whose release is read there is sent its
//! release, without flags, as it is read, so before any later press of it. All of this comes
//! before the RELEASED that follows: on STOP, on the emergency chord, and on
//! BACKGROUND for a sleep. A tap the virtual keyboard did not take is not sent, so on
//! VIRTUAL_KEYBOARD none is, since none goes out. A client the helper drops, whose connection has
//! ended, or whose session is not in front, as on BACKGROUND for another session and on
//! SESSION_UNKNOWN, is sent none: what went to another session is none of its business.
//!
//! After the kernel has dropped events for a keyboard, the helper deals with nothing more of what
//! it read with the loss, asks the kernel which keys are down, and compares the answer with the
//! keys on that keyboard it has told the client are down, by a press not yet followed by its
//! release. It sends, before any KEY for an event read after the answer:
//!
//! - for each key told down that the kernel has up, in rising order of code, a release flagged
//!   [`KEY_SYNTHETIC`] alone;
//! - then for each key the kernel has down that was not told down and that modifies others, one
//!   of [`MODIFIERS`](crate::keys::MODIFIERS) or
//!   [`READER_MODIFIERS`](crate::keys::READER_MODIFIERS), in rising order of code, a press flagged
//!   [`KEY_ASK`] and [`KEY_SYNTHETIC`]; the numpad's Insert only while the Num Lock light is off,
//!   since with it on the key is 0. Rising order of code is not the order the keys went down,
//!   which cannot be known. Such a press wants a verdict, which [`VERDICT_TIMEOUT`], WAIT,
//!   EXPIRED and the run of unanswered presses govern as for any press, and a press read after it
//!   waits for it in line as behind any press. It went down at a moment that cannot be known,
//!   perhaps before keys now taken as down; every key read after it was typed under it, for as
//!   long as its release has not come. Its release comes as for any key: without flags once read,
//!   or flagged [`KEY_SYNTHETIC`] alone if it is lost to a further loss of events or its keyboard
//!   vanishes. Let go of after the answer, it is a press and a release with nothing between. What
//!   reaches the virtual keyboard depends on the key:
//!   - one of the [`MODIFIERS`](crate::keys::MODIFIERS) but Caps Lock, passed, is held back from
//!     the virtual keyboard until a press goes out there, and goes there just before that press;
//!     several held back go together, in the order told. Let go of before then, it never goes
//!     there at all, nor its release, so that a modifier whose combination was lost with the
//!     loss, Alt of Alt+F4, does not reach the program alone. Until then the client has it down
//!     and the virtual keyboard has not; as the helper lets go it is simply dropped;
//!   - Caps Lock, passed, goes on the virtual keyboard at once, as any press: its press is what
//!     toggles the lock, as when it is typed by hand, and the light follows from the desktop;
//!   - Insert and the numpad's Insert, modifiers to the screen reader alone and keys like any
//!     other to a program, never go on the virtual keyboard, nor their release: the verdict on
//!     such a press is taken, and changes nothing there, since typed late it would come under
//!     whatever modifiers the answer found with it, Shift+Insert for Insert pressed first;
//! - and nothing for any other key the kernel has down that was not told down: neither its
//!   repeats nor its release are sent, and a later press of it is sent as any press.
//!
//! The answer can count a press the helper reads only after it, since the kernel sets a key's bit
//! as the driver reports it and hands the event to readers only at the end of the packet. Such a
//! press is sent as an ordinary press, unless a made-up press of that key was just sent, in which
//! case nothing more is. A key typed during the loss is lost, and so is one held across it that
//! does not modify others, until it is pressed again.
//!
//! The emergency chord, Backspace, Escape and Enter (the main one or the numpad's) held down
//! together on the keyboards the helper holds, is read by the helper itself before it does anything else with a key. It lets go of
//! every keyboard, drops the keys still waiting for a verdict, and sends RELEASED EMERGENCY. It
//! works however the client is behaving, and the helper captures again only on a new START. The
//! user pressed it because something was wrong, so a client must not send that START by itself:
//! only when the user asks for kernel capture again, or once the client has been restarted.
//!
//! # Versions and what comes later
//!
//! HELLO names the one protocol version the client speaks. The helper serves only
//! [`PROTOCOL_VERSION`] and turns any other away with GOODBYE VERSION, which carries the version it
//! does speak, so that a client that knows several can connect again with that one. A version's
//! messages never change; a change that would break a client gets a new version, which a helper
//! may serve beside the old one.
//!
//! Additions within a version come as features. HELLO asks for a set of feature bits and WELCOME
//! grants those the helper knows. The helper sends a message type that a feature brings only to a
//! client it granted that feature, and a client sends one only once the feature is granted; any
//! other type is malformed, and the helper ends the connection over it. A client should still
//! ignore a helper message of a type it does not know.
//!
//! Version 1 defines one feature, [`FEATURE_WAIT`]. With it granted, the client may answer a press
//! still waiting for its verdict with VERDICT [`VERDICT_WAIT`], which holds it for longer: the
//! press's bound becomes [`VERDICT_WAIT_TIMEOUT`] from its reading instead of [`VERDICT_TIMEOUT`],
//! for when the client cannot yet tell whether it wants the key, as when its reader has not yet
//! followed the focus. Everything else about the press is as before:
//! - Only the first WAIT for a press counts. A WAIT for a press that is not waiting (answered,
//!   passed on at its bound, or let go of) changes nothing, as a late VERDICT does not.
//! - The press is still answered with keep or pass, at any time before its longer bound; without
//!   an answer by then it is passed on and EXPIRED is sent, as at the usual bound.
//! - The order rule of point 5 holds: presses read after it wait behind it, each with its own bound
//!   from its own reading, and each may itself be answered, WAIT included, while it waits; releases
//!   wait behind it or go ahead of it as point 5 says.
//! - WAIT is no answer to the rule of UNANSWERED_SPAN: a press passed on at its bound after a WAIT
//!   counts as unanswered, so a client that answers only WAIT is dropped by it as one that says
//!   nothing is, when it holds up typing for UNANSWERED_SPAN.
//! - The emergency chord is read as each key comes, before it goes in line, so no press held by
//!   WAIT delays it.
//! - Heartbeats and every other bound are as before.
//!
//! A client that sends VERDICT 2 without FEATURE_WAIT granted is dropped as for any malformed
//! message. A helper without the feature grants it not, and the client answers within
//! VERDICT_TIMEOUT as it would anyway.
//!
//! # Typing, the late pass and the virtual keyboard seen
//!
//! Three more features of version 1 are specified here, and this helper grants none of them yet:
//! [`FEATURE_TYPING`], [`FEATURE_LATE_PASS`] and [`FEATURE_VIRTUAL_SEEN`] are not in
//! [`FEATURES_KNOWN`], so a client may ask for them and WELCOME leaves them out. Until WELCOME
//! grants a feature its messages are malformed, as for any other: the helper ends the connection of
//! a client that sends TYPE or LATE_PASS, and sends no TYPED or VIRTUAL. A client written from this
//! text asks for what it wants, and sends a feature's messages only once WELCOME has granted it.
//!
//! ## Typing, with FEATURE_TYPING
//!
//! For keys the client sends rather than reads: Remote Access, and the screen reader's own sent
//! keys, in a session where it has no other way to type. TYPE: a tag of the client's choosing, the
//! evdev key code, the value, 1 to press or 0 to release, and the mode:
//! - [`TYPE_DECIDE`]: the key goes in line as a key of the pseudo-device [`TYPED_DEVICE`], as though
//!   read from a keyboard when the TYPE was, and comes back as KEY flagged [`KEY_TYPED`], a press
//!   flagged [`KEY_ASK`] too, to be decided as any key is: a press the client keeps does not go out,
//!   and one it passes does, its repeats and release following it. So a chord of the client's own,
//!   typed from another computer, is the client's command.
//! - [`TYPE_INJECT`]: the key goes in line already passed, and no KEY comes back for it.
//!
//! Either way it waits its turn by the rules of point 5, as a key of TYPED_DEVICE: a typed press goes
//! out only once every press read before it has been decided, a typed release of a key that is not a
//! modifier may go ahead of presses still waiting, and the release of a modifier keeps its place. A
//! key typed down and the same key held down on a keyboard are one key to the virtual keyboard, which
//! has it up only once both are let go of, as the X server has a key typed through XTEST that is
//! already held.
//!
//! Each TYPE is answered by one TYPED: its tag, the sequence number of the KEY it was sent as (0 when
//! none was), and the outcome:
//! - [`TYPED_OUT`]: it went on the virtual keyboard;
//! - [`TYPED_KEPT`]: it was sent as KEY and the client kept it, or, for a release, kept its press;
//! - [`TYPED_REFUSED_STATE`]: the helper is not capturing for a client whose session is in front;
//! - [`TYPED_REFUSED_KEY`]: the helper does not type that code, or not at that moment, by the rules
//!   below;
//! - [`TYPED_REFUSED_FULL`]: too many typed keys are waiting, or have come too fast;
//! - [`TYPED_NOT_DOWN`]: a release of a key the client has not typed down;
//! - [`TYPED_LOST`]: the helper let go before the key went out, and it never will.
//!
//! A refused key goes nowhere and takes no place in line. TYPED comes as soon as the key's fate is
//! known: at once for a refusal, as the key goes out or is kept, and for a lost one as the helper
//! lets go, before the RELEASED that follows.
//!
//! What the helper types, and when:
//! - Only while it captures for a client whose session is in front, with its virtual keyboard taken
//!   up. This is checked again as each typed key goes out, not only as its TYPE comes, so that a key
//!   typed for one session never lands in another's, a login screen's or a console. After STOP,
//!   after the emergency chord, while the virtual keyboard settles and in the background the answer
//!   is TYPED_REFUSED_STATE. How many keyboards are held does not matter, so typing works where the
//!   only keyboard is one the helper leaves alone.
//! - Only the codes that source/linuxPlatform/keys/keyMap.py names in its FIXED, NUMPAD and LAYOUT
//!   tables, without KEY_SLEEP (142). Never KEY_POWER (116), KEY_SUSPEND (205), KEY_POWER2 (0x164)
//!   or KEY_RESTART (0x198): logind takes the virtual keyboard's power keys as the machine's.
//! - Never a press of SysRq (99) while an Alt is down on the virtual keyboard, nor of an Alt while
//!   SysRq is down there, so that no typed key reaches the kernel's SysRq handler.
//! - At most [`TYPED_WAITING_LIMIT`] typed keys in line at once, and no more than
//!   [`TYPED_PER_SECOND`] a second over time, in bursts of up to [`TYPED_BURST`].
//! - Whenever the helper lets go, for whatever reason, typed keys not yet out are dropped and
//!   answered TYPED_LOST, never put out, unlike the user's own keys still waiting: a key sent for
//!   one moment must not land in the next. Keys typed down are released with every other key the
//!   virtual keyboard has down.
//! - Typed keys never count toward the emergency chord, nor against it.
//! - An unanswered TYPE_DECIDE press counts toward UNANSWERED_SPAN as any press does, and WAIT, with
//!   FEATURE_WAIT, holds it as it holds any press.
//!
//! A value other than 0 or 1, a mode other than these two, or a code the virtual keyboard cannot
//! send ([`is_virtual_key`](crate::keys::is_virtual_key)) is malformed and ends the connection. A
//! key the rules above refuse is answered with TYPED and the connection is kept, since dropping the
//! client would let go of the keyboards.
//!
//! ## The late pass, with FEATURE_LATE_PASS
//!
//! For a modifier the client kept, such as Alt held with the screen reader's own modifier key, that
//! a key it then passes needs: Alt+Tab, when Alt went down under the screen reader's key. LATE_PASS
//! names a kept press and then a press waiting for its verdict. The kept key goes on the virtual
//! keyboard immediately before the waiting press, in that press's place in line and whatever
//! becomes of it, and its release, once read, is passed as though its press had been. The client
//! sends it just before the VERDICT that passes the waiting press.
//! - The first must be a press the client kept, of one of the
//!   [`MODIFIERS`](crate::keys::MODIFIERS) but Caps Lock, whose key is still down and which no
//!   earlier LATE_PASS named; the second must be waiting for its verdict. Anything else is
//!   malformed.
//! - If the helper lets go before the waiting press's turn, the kept key never goes out.
//!
//! TYPE_INJECT would not do: an injected key joins the end of the line, after the press it has to
//! come before. LATE_PASS also puts out no key but one the user is holding.
//!
//! ## The virtual keyboard seen, with FEATURE_VIRTUAL_SEEN
//!
//! Whether the desktop took up the virtual keyboard, where nothing else tells: a compositor that
//! opens a new keyboard writes the seat's lock lights to it. The kernel passes a light on to a device
//! only when it changes, so for a client granted the feature, a virtual keyboard made after its
//! START starts with every light the opposite of the real keyboards', for the compositor's write to
//! change them; the helper ignores the echo of its own write, and puts the real lights on the
//! virtual keyboard at [`VIRTUAL_SETTLE`]. VIRTUAL then says, once for each client, before any
//! keyboard is grabbed for it:
//! - [`VIRTUAL_SEEN`]: the compositor wrote the lights before VIRTUAL_SETTLE;
//! - [`VIRTUAL_UNSEEN`]: nothing came by then;
//! - [`VIRTUAL_OLD`]: the virtual keyboard was made before this client's START, so nothing can be
//!   seen.
//!
//! SEEN proves the keyboard is read. UNSEEN proves nothing on a desktop that never writes the
//! lights, and means something only on one that was seen to before. The feature is built only for
//! desktops where that write is shown to come, and may never be granted.
//!
//! # Time bounds
//!
//! Every wait the helper makes has a bound, named here with the reasoning for its value. WELCOME
//! carries the ones the client has to keep, so that it need not copy them.

use std::fmt;
use std::time::Duration;

pub const SOCKET_PATH: &str = "/run/linda-keyd/socket";
pub const POLKIT_ACTION: &str = "io.github.mudb0y.linda.keyd.capture";
pub const PROTOCOL_VERSION: u16 = 1;

/// The longest the helper waits for the verdict on a press before it passes the key on by itself.
/// Every key the client does not keep waits for its answer, so this is the most a slow client can
/// add to a keystroke, while the usual cost is one round trip over the socket and the client's own
/// decision, well under a millisecond plus whatever the client takes. A hundred milliseconds is
/// what Linda's X route already allows NVDA for a captured key (CAPTURE_TIMEOUT in
/// source/linuxPlatform/keys/x11.py), and it is about where a delay in typing starts to be felt. It
/// is also far beyond any delay of a working client, its garbage collector and Python's five
/// millisecond thread switch included, so a key that reaches it means the client is in trouble,
/// not merely busy. Each key's bound runs from its own reading, so no key ever waits longer than
/// this however many are queued before it.
pub const VERDICT_TIMEOUT: Duration = Duration::from_millis(100);
/// The longest the helper holds a press the client answered with WAIT, from the press's reading,
/// before it passes the key on by itself. The X route holds a key up to 100 ms for NVDA's answer and
/// up to half a second for NVDA's focus to follow the X focus into a document (DOCUMENT_WAIT in
/// source/linuxPlatform/keys/x11.py); the client's own clock starts a little after the helper read
/// the key, and 600 ms covers both with room. It is also well inside the watchdog's bound on a key
/// in line, STALL_LIMIT and VERDICT_TIMEOUT from its reading, so that a held key is never taken for
/// a stall. Not carried in WELCOME, whose layout is fixed in version 1: a client that asks for
/// FEATURE_WAIT takes it from here.
pub const VERDICT_WAIT_TIMEOUT: Duration = Duration::from_millis(600);
/// The longest the client may stay silent once admitted. Half a second is the interval Linda's X
/// route feeds its own guard at (HEARTBEAT_INTERVAL in x11.py), and cheap at four messages to the
/// timeout.
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_millis(500);
/// Silence from the client after which the helper lets go of everything and drops it. Two seconds
/// is what Linda's X guard waits (GUARD_SILENCE in x11.py): four missed heartbeats, so a client
/// held up by a garbage collection or a burst of disk work is not thrown off, yet short enough
/// that a hung reader leaves keys delayed rather than lost for no more than two seconds, since
/// meanwhile each key is passed on after VERDICT_TIMEOUT anyway.
pub const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(2);
/// The rule that drops a client which is alive but keeps failing to answer, which would otherwise
/// leave every key a tenth of a second late and every one of its commands run twice, by the client
/// and by the application. Taking presses in the order they were read:
/// - a press passed on at its bound, unanswered, joins the run of the press before it when there is
///   one and it was read no more than [`UNANSWERED_GAP`] before this one; otherwise it starts a new
///   run;
/// - a press answered in time ends the run, as does letting go;
/// - when a press joins a run so that the run spans this long, from the reading of its first press
///   to the bound of this one, the helper lets go and drops the client.
///
/// A second is long enough for a pause of a working client, such as a garbage collection over a
/// burst of fast typing, not to be taken for a hang.
pub const UNANSWERED_SPAN: Duration = Duration::from_secs(1);
/// The longest time between the readings of two unanswered presses that keeps them in one run. One
/// second, as the span: while someone types, keys come far closer together than that, so a client
/// that fails every key is dropped after a second of typing, while two failures further apart are
/// two separate slow moments of a working client, which cost those two keys their tenth of a second
/// and nothing more. A client that has hung outright is dropped by the heartbeat; one that fails
/// only an occasional key between pauses of more than a second keeps its place, with each such key
/// passed on late.
pub const UNANSWERED_GAP: Duration = Duration::from_secs(1);
/// How long a new connection has to send HELLO. One client at a time is served, so a connection
/// that says nothing would otherwise keep the place of the client that should have it.
pub const HELLO_TIMEOUT: Duration = Duration::from_secs(2);
/// The longest the helper waits for logind and polkit to admit a client before it turns it away.
/// polkit may have to be started by the bus and its rules engine run on the first question, which
/// can take a second or two on a loaded machine; a client turned away keeps the X route and may
/// ask again.
pub const ADMISSION_TIMEOUT: Duration = Duration::from_secs(5);
/// The bound on each call to logind or polkit, so that a hung service holds up neither the
/// admission of the next client nor the following of the active session for long.
pub const DBUS_CALL_TIMEOUT: Duration = Duration::from_secs(3);
/// How often the helper asks logind which session is in front even without being told of a
/// change, in case a signal is lost. Within this time a missed switch of sessions is caught, after
/// which the keys of a session that is no longer in front stop going to its client.
pub const SESSION_RECHECK: Duration = Duration::from_secs(2);
/// How long the helper waits before connecting to the system bus again after losing it.
pub const DBUS_RETRY: Duration = Duration::from_secs(2);
/// How long a new virtual keyboard has to appear, as an event node udev has processed, before the
/// helper gives it up and never grabs on it. udev takes milliseconds; five seconds leaves room for a
/// machine that is busy starting up.
pub const VIRTUAL_APPEAR_TIMEOUT: Duration = Duration::from_secs(5);
/// How long after the virtual keyboard has appeared the helper waits before the first grab, for
/// the X server or the compositor to open it. A key passed on before then would be lost, and its
/// release might come to a desktop that saw the press on the real keyboard. The kernel's own uinput
/// example waits one second for this. It is paid once in the helper's life, when the first client
/// starts capturing.
pub const VIRTUAL_SETTLE: Duration = Duration::from_secs(1);
/// How often the helper looks again whether every key of a keyboard it wants to grab is up. A key
/// down as the grab is taken would stay down in the desktop for ever, since its release would come
/// to the helper alone, so a keyboard is grabbed only when all its keys are up, however long that
/// takes. Fifty milliseconds is shorter than a keystroke.
pub const KEYS_UP_RETRY: Duration = Duration::from_millis(50);
/// How often the helper tries again to grab a keyboard another program holds.
pub const BUSY_RETRY: Duration = Duration::from_secs(2);
/// How often, while capturing, the helper looks for keyboards that came or went in case a
/// notification of the device directory was missed.
pub const RESCAN_INTERVAL: Duration = Duration::from_secs(5);
/// The longest the helper's loop sleeps while it holds a keyboard, so that the watchdog sees it
/// turn over even when no key comes.
pub const LOOP_TICK: Duration = Duration::from_millis(200);
/// How long the helper's loop may go without turning over, or the oldest key in line may stay
/// undecided beyond its verdict bound, while a keyboard is held, before the watchdog thread lets
/// go of every keyboard and ends the helper. Five ticks of the loop, so that a loop held up by the
/// scheduler under load is not taken for a hung one; well short of the systemd watchdog, which
/// covers the watchdog thread itself.
pub const STALL_LIMIT: Duration = Duration::from_secs(1);
/// How long after logind has said the machine woke a late wake is still taken for the sleep's: the
/// processes thaw before logind says so, and the loop may be woken late once more after it.
pub const SLEEP_GRACE: Duration = Duration::from_secs(2);
/// How often a freeze of the whole process that logind did not announce is forgiven: once, so that
/// a sleep that went round logind does not count against the client or make the watchdog thread end
/// the helper, and not again within this, so that a machine starved of the processor, which wakes
/// every thread late at every turn, gains a hung client a single late wake and no more.
pub const FREEZE_FORGIVEN_EVERY: Duration = Duration::from_secs(60);
/// systemd's watchdog timeout while the machine sleeps, set before sleep and put back on waking,
/// when the helper holds nothing. Ten minutes: the processes are frozen only while devices suspend
/// and resume, seconds and not minutes, and a helper that hangs meanwhile is still caught.
pub const SLEEP_WATCHDOG: Duration = Duration::from_secs(600);
/// How often the watchdog thread looks at the loop.
pub const WATCHDOG_CHECK: Duration = Duration::from_millis(100);
/// A sleep of WATCHDOG_CHECK that the watchdog thread finds took longer than this means the whole
/// process was stopped, not the loop alone: frozen while the machine suspended and resumed, which
/// the monotonic clock counts, or starved of the processor. Every thread wakes together then, so
/// the loop is given STALL_LIMIT afresh from that moment rather than judged by a turn it could not
/// have made. Half of STALL_LIMIT, five times the sleep, is beyond any ordinary lateness of a
/// sleeping thread.
pub const WATCHDOG_FROZEN: Duration = Duration::from_millis(500);

/// The most connections at once that have not been admitted yet. Any more are closed at once.
pub const MAX_CALLERS: usize = 4;
/// The most connections the loop accepts in one turn; the rest wait in the socket's backlog for the
/// next. Any local user may connect, and each connection costs the loop an accept, a look at the
/// caller's credentials and often a refusal, so without a bound a few processes connecting without
/// pause would keep the loop from the keyboards until the watchdog ended the helper. As many as may
/// wait to be admitted, so that a burst of rightful callers is not held up.
pub const ACCEPTS_PER_TURN: usize = MAX_CALLERS;
/// The most refusals the log has a line each for within [`REFUSAL_LOG_SPAN`]; the rest are counted,
/// and the count is logged when the span ends. Any local user can be turned away as often as they
/// connect, and a line each would let them use up the journal's allowance for the helper, so that
/// what it logs about the keyboards would be lost. Ten is more than a client retrying ever needs.
pub const REFUSALS_LOGGED: usize = 10;
pub const REFUSAL_LOG_SPAN: Duration = Duration::from_secs(60);
/// The most messages the helper keeps for a client that is not reading them, beyond what the
/// socket holds; past this the client is taken for hung and dropped.
pub const OUTBOX_LIMIT: usize = 512;
/// The largest message either side sends, text included.
pub const MAX_MESSAGE: usize = 256;

/// TYPE and TYPED: the client types keys through the virtual keyboard. Specified, and not yet granted
/// by this helper.
pub const FEATURE_TYPING: u32 = 1;
/// VERDICT [`VERDICT_WAIT`]: the client may hold a press for its answer up to
/// [`VERDICT_WAIT_TIMEOUT`] instead of [`VERDICT_TIMEOUT`].
pub const FEATURE_WAIT: u32 = 2;
/// LATE_PASS: a kept modifier goes on the virtual keyboard ahead of a press the client passes.
/// Specified, and not yet granted by this helper.
pub const FEATURE_LATE_PASS: u32 = 4;
/// VIRTUAL: whether the desktop wrote the lights of a new virtual keyboard. Specified, and not yet
/// granted by this helper.
pub const FEATURE_VIRTUAL_SEEN: u32 = 8;
/// The features this helper grants.
pub const FEATURES_KNOWN: u32 = FEATURE_WAIT;
/// The device number of keys the client types with [`TYPE_DECIDE`]. Real keyboards are numbered as
/// the kernel numbers input devices, which never comes near this.
pub const TYPED_DEVICE: u32 = u32::MAX;
/// The most typed keys in line at once, with [`FEATURE_TYPING`]; beyond it TYPE is answered
/// [`TYPED_REFUSED_FULL`]. Someone typing from another computer has a few keys on the way at a time,
/// and the line has no other bound, so this keeps a client gone wrong from filling the helper's
/// memory with keys that would all go out.
pub const TYPED_WAITING_LIMIT: usize = 64;
/// How many typed keys a second the helper takes over time, presses and releases each counted, and
/// how many it takes at once after a pause: a bucket of [`TYPED_BURST`] filled at this rate, a TYPE
/// that finds it empty answered [`TYPED_REFUSED_FULL`]. Far beyond anyone typing, even a short
/// string sent at once, yet a client gone wrong cannot type pages a second into whatever has the
/// focus.
pub const TYPED_PER_SECOND: u32 = 100;
pub const TYPED_BURST: u32 = 200;

/// The press wants a verdict.
pub const KEY_ASK: u8 = 0x01;
/// Made by the helper rather than read from the keyboard: the release of a key held down on a
/// keyboard that vanished, or whose events the kernel dropped, or, with [`KEY_ASK`], the press of
/// a modifier found down after the kernel dropped events.
pub const KEY_SYNTHETIC: u8 = 0x02;
/// A key the client typed with TYPE [`TYPE_DECIDE`], with [`FEATURE_TYPING`].
pub const KEY_TYPED: u8 = 0x04;

pub const KEY_RELEASE: u8 = 0;
pub const KEY_PRESS: u8 = 1;
pub const KEY_REPEAT: u8 = 2;

/// Lock light bits in LOCKS: bit n is the kernel's LED code n.
pub const LOCK_NUM: u16 = 1 << 0;
pub const LOCK_CAPS: u16 = 1 << 1;
pub const LOCK_SCROLL: u16 = 1 << 2;

// The client's messages.
/// `<BHI`: type, version, features asked for.
pub const HELLO: u8 = 0x01;
/// `<B`.
pub const START: u8 = 0x02;
/// `<B`.
pub const STOP: u8 = 0x03;
/// `<BQB`: type, sequence number of the press, 1 to keep it or 0 to pass it on; with
/// [`FEATURE_WAIT`], [`VERDICT_WAIT`] to hold it longer.
pub const VERDICT: u8 = 0x04;
/// The value of VERDICT that holds a press until [`VERDICT_WAIT_TIMEOUT`], with [`FEATURE_WAIT`].
pub const VERDICT_WAIT: u8 = 2;
/// `<BI`: type, a counter of the client's choosing, echoed back.
pub const HEARTBEAT: u8 = 0x05;
/// `<BIHBB`, with [`FEATURE_TYPING`]: type, a tag of the client's choosing, the evdev key code, 1 to
/// press or 0 to release, and the mode, [`TYPE_DECIDE`] or [`TYPE_INJECT`].
pub const TYPE: u8 = 0x06;
/// TYPE's mode in which the key comes back as KEY, flagged [`KEY_TYPED`], to be decided.
pub const TYPE_DECIDE: u8 = 0;
/// TYPE's mode in which the key goes in line already passed.
pub const TYPE_INJECT: u8 = 1;
/// `<BQQ`, with [`FEATURE_LATE_PASS`]: type, the sequence number of a kept press whose key is still
/// down, then that of the press waiting for its verdict which it goes out just before.
pub const LATE_PASS: u8 = 0x07;

// The helper's messages.
/// `<BHIHHHH`: type, version, features granted, then in milliseconds VERDICT_TIMEOUT,
/// HEARTBEAT_INTERVAL, HEARTBEAT_TIMEOUT and UNANSWERED_SPAN.
pub const WELCOME: u8 = 0x81;
/// `<BBH` and text: type, reason, the protocol version the helper speaks, then what happened in
/// words, for the client's log.
pub const GOODBYE: u8 = 0x82;
/// `<BQIHBB`: type, sequence number, device, key code, value, flags. The sequence number counts
/// the KEYs the helper sends this client, from 1. The device is the kernel's
/// number of the input device (N in /sys/class/input/inputN). The code is the kernel's evdev key
/// code, an X key code less 8. The value is [`KEY_RELEASE`], [`KEY_PRESS`] or [`KEY_REPEAT`]; the
/// flags are [`KEY_ASK`], [`KEY_SYNTHETIC`] and [`KEY_TYPED`].
pub const KEY: u8 = 0x83;
/// `<BQ`: type, sequence number of a press passed on without a verdict.
pub const EXPIRED: u8 = 0x84;
/// `<BB`: type, reason.
pub const RELEASED: u8 = 0x85;
/// `<BIBB` and the device's name: type, device, status, and for IGNORED why.
pub const DEVICE: u8 = 0x86;
/// `<BH`: type, the lock lights that are on, [`LOCK_NUM`], [`LOCK_CAPS`], [`LOCK_SCROLL`] and the
/// other LED codes' bits. They are what the X server, the compositor or the console, whichever owns
/// the locks, last set on the helper's virtual keyboard, which the helper also shows on the real
/// keyboards' lights. It comes only while the client's session is in front and of lights that
/// session set, and again when the helper captures again, with what changed meanwhile.
pub const LOCKS: u8 = 0x87;
/// `<BI`: type, the counter of the client's HEARTBEAT.
pub const HEARTBEAT_ECHO: u8 = 0x88;
/// `<BIQB`, with [`FEATURE_TYPING`]: type, the tag of the TYPE it answers, the sequence number of
/// the KEY that TYPE was sent as or 0, and the outcome, one of the TYPED_ values.
pub const TYPED: u8 = 0x89;
/// TYPED's outcomes, as the module's text gives them.
pub const TYPED_OUT: u8 = 1;
pub const TYPED_KEPT: u8 = 2;
pub const TYPED_REFUSED_STATE: u8 = 3;
pub const TYPED_REFUSED_KEY: u8 = 4;
pub const TYPED_REFUSED_FULL: u8 = 5;
pub const TYPED_NOT_DOWN: u8 = 6;
pub const TYPED_LOST: u8 = 7;
/// `<BB`, with [`FEATURE_VIRTUAL_SEEN`]: type, and [`VIRTUAL_SEEN`], [`VIRTUAL_UNSEEN`] or
/// [`VIRTUAL_OLD`].
pub const VIRTUAL: u8 = 0x8A;
pub const VIRTUAL_SEEN: u8 = 1;
pub const VIRTUAL_UNSEEN: u8 = 2;
pub const VIRTUAL_OLD: u8 = 3;

/// Why the helper let go of every keyboard.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Released {
    /// The client sent STOP.
    Stopped = 1,
    /// The emergency chord.
    Emergency = 2,
    /// The client's session is no longer the one in front. The helper lets go the moment the
    /// kernel puts in front another console than that of the session logind last named, before
    /// logind has said what changed, so that nothing typed on another console, a password perhaps,
    /// comes to the client. It captures again by itself only once logind says the client's session
    /// is in front, never on the kernel's word alone.
    ///
    /// The same comes when logind says the machine is going to sleep, and the helper captures again
    /// by itself once logind says it has woken, if the client's session is in front. Every process
    /// may have been frozen for seconds in between, the client's among them, which the helper does
    /// not hold against the client's heartbeat; a client should not hold it against the helper's
    /// echoes either.
    Background = 3,
    /// The virtual keyboard could not be made, did not appear, or stopped taking keys. START tries
    /// to make it again.
    VirtualKeyboard = 4,
    /// The helper cannot tell which session is in front, because it lost the system bus or logind.
    /// It captures again by itself once it can.
    SessionUnknown = 5,
}

/// Why the helper is closing the connection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Goodbye {
    /// The client speaks another version; the message carries the one the helper speaks.
    Version = 1,
    /// The client is not a process of the session in front on seat0.
    NotInFront = 2,
    /// polkit does not allow the client the action.
    NotAuthorized = 3,
    /// Another client of the session in front is being served.
    Busy = 4,
    /// logind or polkit could not be asked, or did not answer in time.
    NoAnswer = 5,
    /// No HELLO in time, or no message for HEARTBEAT_TIMEOUT.
    Silent = 6,
    /// The client sent a message the protocol does not allow; the text says which.
    Malformed = 7,
    /// The client left presses unanswered for UNANSWERED_SPAN.
    Unanswered = 8,
    /// A client of the session now in front took this one's place.
    Replaced = 9,
    /// The client did not read what the helper sent it.
    Overflow = 10,
    /// Too many connections were waiting to be admitted.
    TooMany = 11,
}

/// What became of a keyboard.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum DeviceStatus {
    /// Grabbed: its keys come to the client.
    Held = 1,
    /// A key on it is down, and it is grabbed once all are up.
    Waiting = 2,
    /// Another program holds it; the helper tries again every BUSY_RETRY.
    Busy = 3,
    /// It vanished. Every key it held down has been released, and the client has been sent a
    /// release, flagged SYNTHETIC, for each.
    Gone = 4,
    /// It looks like a keyboard but the helper will not take it; the detail says why.
    Ignored = 5,
}

/// Why a keyboard is IGNORED.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Ignored {
    /// It is also a pointer or a touch device, which the helper does not pass on.
    Pointer = 1,
    /// It also has switches, such as a tablet mode switch.
    Switches = 2,
    /// It has buttons the virtual keyboard cannot pass on.
    Buttons = 3,
    /// It is a virtual device of some other program, such as another key remapper's, which the
    /// helper never grabs so that two such programs cannot feed each other's keys round for ever.
    Virtual = 4,
    /// It belongs to another seat.
    OtherSeat = 5,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FromClient {
    Hello {
        version: u16,
        features: u32,
    },
    Start,
    Stop,
    Verdict {
        seq: u64,
        keep: bool,
    },
    /// VERDICT [`VERDICT_WAIT`], once [`FEATURE_WAIT`] is granted.
    Wait {
        seq: u64,
    },
    Heartbeat {
        counter: u32,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Key {
    pub seq: u64,
    pub device: u32,
    pub code: u16,
    pub value: u8,
    pub flags: u8,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToClient {
    Welcome {
        version: u16,
        features: u32,
        verdict_ms: u16,
        heartbeat_interval_ms: u16,
        heartbeat_timeout_ms: u16,
        unanswered_span_ms: u16,
    },
    Goodbye {
        reason: Goodbye,
        version: u16,
        text: String,
    },
    Key(Key),
    Expired {
        seq: u64,
    },
    Released {
        reason: Released,
    },
    Device {
        device: u32,
        status: DeviceStatus,
        detail: u8,
        name: String,
    },
    Locks {
        leds: u16,
    },
    HeartbeatEcho {
        counter: u32,
    },
}

/// A message that breaks the protocol, with what was wrong in words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Malformed(pub String);

impl fmt::Display for Malformed {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl ToClient {
    /// The WELCOME this helper sends.
    pub fn welcome(features: u32) -> ToClient {
        ToClient::Welcome {
            version: PROTOCOL_VERSION,
            features,
            verdict_ms: millis(VERDICT_TIMEOUT),
            heartbeat_interval_ms: millis(HEARTBEAT_INTERVAL),
            heartbeat_timeout_ms: millis(HEARTBEAT_TIMEOUT),
            unanswered_span_ms: millis(UNANSWERED_SPAN),
        }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut b = Vec::with_capacity(24);
        match self {
            ToClient::Welcome {
                version,
                features,
                verdict_ms,
                heartbeat_interval_ms,
                heartbeat_timeout_ms,
                unanswered_span_ms,
            } => {
                b.push(WELCOME);
                b.extend_from_slice(&version.to_le_bytes());
                b.extend_from_slice(&features.to_le_bytes());
                for ms in [
                    verdict_ms,
                    heartbeat_interval_ms,
                    heartbeat_timeout_ms,
                    unanswered_span_ms,
                ] {
                    b.extend_from_slice(&ms.to_le_bytes());
                }
            }
            ToClient::Goodbye {
                reason,
                version,
                text,
            } => {
                b.push(GOODBYE);
                b.push(*reason as u8);
                b.extend_from_slice(&version.to_le_bytes());
                push_text(&mut b, text);
            }
            ToClient::Key(k) => {
                b.push(KEY);
                b.extend_from_slice(&k.seq.to_le_bytes());
                b.extend_from_slice(&k.device.to_le_bytes());
                b.extend_from_slice(&k.code.to_le_bytes());
                b.push(k.value);
                b.push(k.flags);
            }
            ToClient::Expired { seq } => {
                b.push(EXPIRED);
                b.extend_from_slice(&seq.to_le_bytes());
            }
            ToClient::Released { reason } => {
                b.push(RELEASED);
                b.push(*reason as u8);
            }
            ToClient::Device {
                device,
                status,
                detail,
                name,
            } => {
                b.push(DEVICE);
                b.extend_from_slice(&device.to_le_bytes());
                b.push(*status as u8);
                b.push(*detail);
                push_text(&mut b, name);
            }
            ToClient::Locks { leds } => {
                b.push(LOCKS);
                b.extend_from_slice(&leds.to_le_bytes());
            }
            ToClient::HeartbeatEcho { counter } => {
                b.push(HEARTBEAT_ECHO);
                b.extend_from_slice(&counter.to_le_bytes());
            }
        }
        b
    }

    pub fn decode(m: &[u8]) -> Result<ToClient, Malformed> {
        let mut r = Reader::new(m)?;
        let message = match r.kind {
            WELCOME => {
                r.fixed("WELCOME", 15)?;
                ToClient::Welcome {
                    version: r.u16(),
                    features: r.u32(),
                    verdict_ms: r.u16(),
                    heartbeat_interval_ms: r.u16(),
                    heartbeat_timeout_ms: r.u16(),
                    unanswered_span_ms: r.u16(),
                }
            }
            GOODBYE => {
                r.at_least("GOODBYE", 4)?;
                let reason = r.u8();
                ToClient::Goodbye {
                    reason: goodbye_reason(reason)?,
                    version: r.u16(),
                    text: r.text()?,
                }
            }
            KEY => {
                r.fixed("KEY", 17)?;
                ToClient::Key(Key {
                    seq: r.u64(),
                    device: r.u32(),
                    code: r.u16(),
                    value: r.u8(),
                    flags: r.u8(),
                })
            }
            EXPIRED => {
                r.fixed("EXPIRED", 9)?;
                ToClient::Expired { seq: r.u64() }
            }
            RELEASED => {
                r.fixed("RELEASED", 2)?;
                ToClient::Released {
                    reason: released_reason(r.u8())?,
                }
            }
            DEVICE => {
                r.at_least("DEVICE", 7)?;
                let device = r.u32();
                let status = device_status(r.u8())?;
                ToClient::Device {
                    device,
                    status,
                    detail: r.u8(),
                    name: r.text()?,
                }
            }
            LOCKS => {
                r.fixed("LOCKS", 3)?;
                ToClient::Locks { leds: r.u16() }
            }
            HEARTBEAT_ECHO => {
                r.fixed("HEARTBEAT", 5)?;
                ToClient::HeartbeatEcho { counter: r.u32() }
            }
            t => return Err(Malformed(format!("unknown message type {t:#04x}"))),
        };
        Ok(message)
    }
}

impl FromClient {
    pub fn encode(&self) -> Vec<u8> {
        let mut b = Vec::with_capacity(10);
        match self {
            FromClient::Hello { version, features } => {
                b.push(HELLO);
                b.extend_from_slice(&version.to_le_bytes());
                b.extend_from_slice(&features.to_le_bytes());
            }
            FromClient::Start => b.push(START),
            FromClient::Stop => b.push(STOP),
            FromClient::Verdict { seq, keep } => {
                b.push(VERDICT);
                b.extend_from_slice(&seq.to_le_bytes());
                b.push(u8::from(*keep));
            }
            FromClient::Wait { seq } => {
                b.push(VERDICT);
                b.extend_from_slice(&seq.to_le_bytes());
                b.push(VERDICT_WAIT);
            }
            FromClient::Heartbeat { counter } => {
                b.push(HEARTBEAT);
                b.extend_from_slice(&counter.to_le_bytes());
            }
        }
        b
    }

    pub fn decode(m: &[u8]) -> Result<FromClient, Malformed> {
        let mut r = Reader::new(m)?;
        let message = match r.kind {
            HELLO => {
                r.fixed("HELLO", 7)?;
                FromClient::Hello {
                    version: r.u16(),
                    features: r.u32(),
                }
            }
            START => {
                r.fixed("START", 1)?;
                FromClient::Start
            }
            STOP => {
                r.fixed("STOP", 1)?;
                FromClient::Stop
            }
            VERDICT => {
                r.fixed("VERDICT", 10)?;
                let seq = r.u64();
                match r.u8() {
                    0 => FromClient::Verdict { seq, keep: false },
                    1 => FromClient::Verdict { seq, keep: true },
                    VERDICT_WAIT => FromClient::Wait { seq },
                    v => return Err(Malformed(format!("VERDICT {v} is not 0, 1 or 2"))),
                }
            }
            HEARTBEAT => {
                r.fixed("HEARTBEAT", 5)?;
                FromClient::Heartbeat { counter: r.u32() }
            }
            t => return Err(Malformed(format!("unknown message type {t:#04x}"))),
        };
        Ok(message)
    }
}

fn millis(d: Duration) -> u16 {
    u16::try_from(d.as_millis()).unwrap_or(u16::MAX)
}

/// Text cut to fit [`MAX_MESSAGE`], at a character boundary.
fn push_text(b: &mut Vec<u8>, text: &str) {
    let room = MAX_MESSAGE.saturating_sub(b.len());
    let mut end = text.len().min(room);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    b.extend_from_slice(&text.as_bytes()[..end]);
}

fn goodbye_reason(v: u8) -> Result<Goodbye, Malformed> {
    use Goodbye::*;
    [
        Version,
        NotInFront,
        NotAuthorized,
        Busy,
        NoAnswer,
        Silent,
        Malformed,
        Unanswered,
        Replaced,
        Overflow,
        TooMany,
    ]
    .into_iter()
    .find(|r| *r as u8 == v)
    .ok_or_else(|| self::Malformed(format!("unknown GOODBYE reason {v}")))
}

fn released_reason(v: u8) -> Result<Released, Malformed> {
    use Released::*;
    [
        Stopped,
        Emergency,
        Background,
        VirtualKeyboard,
        SessionUnknown,
    ]
    .into_iter()
    .find(|r| *r as u8 == v)
    .ok_or_else(|| Malformed(format!("unknown RELEASED reason {v}")))
}

fn device_status(v: u8) -> Result<DeviceStatus, Malformed> {
    use DeviceStatus::*;
    [Held, Waiting, Busy, Gone, Ignored]
        .into_iter()
        .find(|s| *s as u8 == v)
        .ok_or_else(|| Malformed(format!("unknown DEVICE status {v}")))
}

struct Reader<'a> {
    m: &'a [u8],
    at: usize,
    kind: u8,
}

impl<'a> Reader<'a> {
    fn new(m: &'a [u8]) -> Result<Self, Malformed> {
        match m.first() {
            None => Err(Malformed("an empty message".to_owned())),
            Some(_) if m.len() > MAX_MESSAGE => Err(Malformed(format!(
                "a message of {} bytes, more than {MAX_MESSAGE}",
                m.len()
            ))),
            Some(&kind) => Ok(Reader { m, at: 1, kind }),
        }
    }

    fn fixed(&self, name: &str, len: usize) -> Result<(), Malformed> {
        if self.m.len() == len {
            Ok(())
        } else {
            Err(Malformed(format!(
                "{name} of {} bytes, which should be {len}",
                self.m.len()
            )))
        }
    }

    fn at_least(&self, name: &str, len: usize) -> Result<(), Malformed> {
        if self.m.len() >= len {
            Ok(())
        } else {
            Err(Malformed(format!(
                "{name} of {} bytes, which should be at least {len}",
                self.m.len()
            )))
        }
    }

    fn take<const N: usize>(&mut self) -> [u8; N] {
        let mut a = [0; N];
        a.copy_from_slice(&self.m[self.at..self.at + N]);
        self.at += N;
        a
    }

    fn u8(&mut self) -> u8 {
        self.take::<1>()[0]
    }

    fn u16(&mut self) -> u16 {
        u16::from_le_bytes(self.take())
    }

    fn u32(&mut self) -> u32 {
        u32::from_le_bytes(self.take())
    }

    fn u64(&mut self) -> u64 {
        u64::from_le_bytes(self.take())
    }

    fn text(&mut self) -> Result<String, Malformed> {
        let rest = &self.m[self.at..];
        self.at = self.m.len();
        String::from_utf8(rest.to_vec()).map_err(|_| Malformed("text that is not UTF-8".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn every_helper_message() -> Vec<ToClient> {
        vec![
            ToClient::welcome(0),
            ToClient::Goodbye {
                reason: Goodbye::Busy,
                version: 1,
                text: "another client".into(),
            },
            ToClient::Key(Key {
                seq: 0x0102_0304_0506_0708,
                device: 7,
                code: 58,
                value: KEY_PRESS,
                flags: KEY_ASK,
            }),
            ToClient::Expired { seq: 9 },
            ToClient::Released {
                reason: Released::Emergency,
            },
            ToClient::Device {
                device: 3,
                status: DeviceStatus::Ignored,
                detail: Ignored::Pointer as u8,
                name: "Logitech K400".into(),
            },
            ToClient::Locks { leds: LOCK_CAPS },
            ToClient::HeartbeatEcho { counter: 42 },
        ]
    }

    #[test]
    fn every_message_comes_back_as_it_went() {
        for m in every_helper_message() {
            assert_eq!(ToClient::decode(&m.encode()), Ok(m.clone()), "{m:?}");
        }
        for m in [
            FromClient::Hello {
                version: 1,
                features: 0,
            },
            FromClient::Start,
            FromClient::Stop,
            FromClient::Verdict {
                seq: u64::MAX,
                keep: true,
            },
            FromClient::Verdict {
                seq: 1,
                keep: false,
            },
            FromClient::Wait { seq: 3 },
            FromClient::Heartbeat { counter: 1 },
        ] {
            assert_eq!(FromClient::decode(&m.encode()), Ok(m.clone()), "{m:?}");
        }
    }

    /// The sizes the module's documentation gives as Python struct formats.
    #[test]
    fn the_layouts_are_the_documented_sizes() {
        let size = |m: &ToClient| m.encode().len();
        let all = every_helper_message();
        assert_eq!(size(&all[0]), 15);
        assert_eq!(size(&all[1]), 4 + "another client".len());
        assert_eq!(size(&all[2]), 17);
        assert_eq!(size(&all[3]), 9);
        assert_eq!(size(&all[4]), 2);
        assert_eq!(size(&all[5]), 7 + "Logitech K400".len());
        assert_eq!(size(&all[6]), 3);
        assert_eq!(size(&all[7]), 5);
        let key = all[2].encode();
        assert_eq!(key[0], KEY);
        assert_eq!(&key[1..9], &[8, 7, 6, 5, 4, 3, 2, 1], "little-endian");
        let hello = FromClient::Hello {
            version: 1,
            features: 0,
        };
        assert_eq!(hello.encode(), vec![HELLO, 1, 0, 0, 0, 0, 0]);
        let verdict = FromClient::Verdict { seq: 2, keep: true };
        assert_eq!(verdict.encode(), vec![VERDICT, 2, 0, 0, 0, 0, 0, 0, 0, 1]);
        let wait = FromClient::Wait { seq: 2 };
        assert_eq!(wait.encode(), vec![VERDICT, 2, 0, 0, 0, 0, 0, 0, 0, 2]);
    }

    #[test]
    fn the_welcome_carries_the_bounds() {
        assert_eq!(
            ToClient::welcome(0),
            ToClient::Welcome {
                version: 1,
                features: 0,
                verdict_ms: 100,
                heartbeat_interval_ms: 500,
                heartbeat_timeout_ms: 2000,
                unanswered_span_ms: 1000,
            }
        );
    }

    #[test]
    fn rubbish_is_refused_with_a_reason() {
        for (m, why) in [
            (&[][..], "empty"),
            (&[0x7f], "unknown message type 0x7f"),
            (&[KEY], "unknown message type 0x83"),
            (&[START, 0], "START of 2 bytes"),
            (&[HELLO, 1, 0], "HELLO of 3 bytes"),
            (&[VERDICT, 1, 0, 0, 0, 0, 0, 0, 0, 3], "not 0, 1 or 2"),
            (&[HEARTBEAT, 1, 2, 3], "HEARTBEAT of 4 bytes"),
        ] {
            let e = FromClient::decode(m).unwrap_err();
            assert!(e.0.contains(why), "{m:?}: {e}");
        }
        let long = vec![HEARTBEAT; MAX_MESSAGE + 1];
        assert!(
            FromClient::decode(&long)
                .unwrap_err()
                .0
                .contains("more than")
        );
        assert!(
            ToClient::decode(&[DEVICE, 1, 0, 0, 0, 1, 0, 0xff])
                .unwrap_err()
                .0
                .contains("UTF-8")
        );
        assert!(ToClient::decode(&[RELEASED, 99]).is_err());
    }

    /// TYPE and LATE_PASS of their documented sizes, 9 and 17 bytes.
    fn planned_client_messages() -> [Vec<u8>; 2] {
        let mut typed = vec![TYPE];
        typed.extend_from_slice(&7u32.to_le_bytes());
        typed.extend_from_slice(&30u16.to_le_bytes());
        typed.extend_from_slice(&[1, TYPE_INJECT]);
        let mut late = vec![LATE_PASS];
        late.extend_from_slice(&1u64.to_le_bytes());
        late.extend_from_slice(&2u64.to_le_bytes());
        [typed, late]
    }

    #[test]
    fn the_planned_features_are_specified_and_not_granted() {
        let features = [
            FEATURE_TYPING,
            FEATURE_WAIT,
            FEATURE_LATE_PASS,
            FEATURE_VIRTUAL_SEEN,
        ];
        assert!(features.iter().all(|f| f.count_ones() == 1));
        assert_eq!(features.iter().fold(0, |all, f| all | f), 0b1111);
        assert_eq!(FEATURES_KNOWN, FEATURE_WAIT);
        let client = [HELLO, START, STOP, VERDICT, HEARTBEAT, TYPE, LATE_PASS];
        let helper = [
            WELCOME,
            GOODBYE,
            KEY,
            EXPIRED,
            RELEASED,
            DEVICE,
            LOCKS,
            HEARTBEAT_ECHO,
            TYPED,
            VIRTUAL,
        ];
        assert!(client.iter().all(|t| *t < 0x80) && helper.iter().all(|t| *t >= 0x80));
        let mut all: Vec<u8> = client.iter().chain(&helper).copied().collect();
        all.sort_unstable();
        all.dedup();
        assert_eq!(all.len(), client.len() + helper.len());
        let [typed, late] = planned_client_messages();
        assert_eq!((typed.len(), late.len()), (9, 17));
        for (m, why) in [
            (typed, "unknown message type 0x06"),
            (late, "unknown message type 0x07"),
        ] {
            assert!(FromClient::decode(&m).unwrap_err().0.contains(why));
        }
    }

    /// A helper built from this tree, asked for every feature, grants FEATURE_WAIT alone, and ends
    /// the connection of a client that then types or late-passes a key.
    #[test]
    fn a_client_that_sends_a_planned_message_is_dropped() {
        use crate::core::{Core, Front, Output, Peer};
        use std::time::Instant;

        let peer = Peer {
            pid: 4242,
            uid: 1000,
            gid: 100,
        };
        for m in planned_client_messages() {
            let world = crate::mock::World::new();
            let mut core = Core::new(world.devices());
            let now = Instant::now();
            core.front_changed(
                Front::Session {
                    id: "2".into(),
                    uid: 1000,
                    vt: 7,
                },
                now,
            );
            core.connected(1, peer, now);
            let hello = FromClient::Hello {
                version: PROTOCOL_VERSION,
                features: FEATURE_TYPING | FEATURE_WAIT | FEATURE_LATE_PASS | FEATURE_VIRTUAL_SEEN,
            };
            core.received(1, &hello.encode(), now);
            assert!(core.take_output().contains(&Output::Admit(1, peer)));
            core.admitted(1, Ok("2".into()), now);
            let granted: Vec<u32> = core
                .take_output()
                .into_iter()
                .filter_map(|o| match o {
                    Output::Send(1, ToClient::Welcome { features, .. }) => Some(features),
                    _ => None,
                })
                .collect();
            assert_eq!(granted, vec![FEATURE_WAIT]);
            core.received(1, &m, now);
            let out = core.take_output();
            assert!(
                out.iter().any(|o| matches!(
                    o,
                    Output::Send(
                        1,
                        ToClient::Goodbye {
                            reason: Goodbye::Malformed,
                            ..
                        }
                    )
                )),
                "{out:?}"
            );
            assert!(out.contains(&Output::Close(1)), "{out:?}");
        }
    }

    #[test]
    fn long_text_is_cut_to_fit_at_a_character() {
        let m = ToClient::Device {
            device: 1,
            status: DeviceStatus::Held,
            detail: 0,
            name: "ą".repeat(400),
        };
        let b = m.encode();
        assert!(b.len() <= MAX_MESSAGE);
        let ToClient::Device { name, .. } = ToClient::decode(&b).unwrap() else {
            panic!()
        };
        assert!(name.chars().all(|c| c == 'ą') && !name.is_empty());
    }
}
