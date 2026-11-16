//! Reading a string property of an X server's root window, which is how the
//! accessibility bus launcher advertises the bus on X11 (`AT_SPI_BUS`). Only
//! what that takes of the core protocol: the local socket a display names,
//! the MIT-MAGIC-COOKIE-1 an Xauthority file holds for it, the connection
//! setup, InternAtom and GetProperty. Every wait is on the runtime, never on
//! the thread, so the caller's deadline holds whatever the server does.

use std::io::{self, Read};
use std::os::linux::net::SocketAddrExt;
use std::os::unix::net::SocketAddr;
use std::path::PathBuf;

use rustix::net::SendFlags;
use zruntime::net::unix::UnixStream;
use zruntime::{Shared, SharedRuntime};

use crate::Error;

/// A local display, as `DISPLAY` names one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Display {
    pub number: u32,
    pub screen: usize,
}

impl Display {
    /// Parses `:N`, `:N.S`, `unix:N` or `unix:N.S`. A display on another
    /// host is None: it would take TCP, and the engine connects to nothing
    /// but a local socket.
    pub fn parse(value: &str) -> Option<Display> {
        let (host, rest) = value.split_once(':')?;
        if !(host.is_empty() || host == "unix") {
            return None;
        }
        let (number, screen) = match rest.split_once('.') {
            Some((n, s)) => (n, Some(s)),
            None => (rest, None),
        };
        let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
        if !digits(number) || !screen.is_none_or(digits) {
            return None;
        }
        Some(Display {
            number: number.parse().ok()?,
            screen: screen.map_or(Some(0), |s| s.parse().ok())?,
        })
    }
}

const COOKIE: &[u8] = b"MIT-MAGIC-COOKIE-1";
const FAMILY_LOCAL: u16 = 256;
const FAMILY_WILD: u16 = 65535;
const ATOM_STRING: u32 = 31;
/// In four-byte units, as GetProperty counts.
const MAX_VALUE: u32 = 4096;
/// No reply this reader asks for comes near this; anything longer is not a
/// server worth reading further.
const MAX_REPLY: usize = 1 << 20;

/// One counted field of an Xauthority entry.
fn counted<'a>(file: &'a [u8], at: &mut usize) -> Option<&'a [u8]> {
    let len = usize::from(u16::from_be_bytes(file.get(*at..*at + 2)?.try_into().ok()?));
    let field = file.get(*at + 2..*at + 2 + len)?;
    *at += 2 + len;
    Some(field)
}

/// The MIT-MAGIC-COOKIE-1 that the Xauthority `file` holds for local display
/// `number` on host `host`, chosen as libXau's `XauGetBestAuthByAddr`
/// chooses it for libxcb: the first entry for this host or for any host,
/// for this display or for any display.
fn cookie(file: &[u8], host: &[u8], number: u32) -> Option<Vec<u8>> {
    let number = number.to_string();
    let mut at = 0;
    while at < file.len() {
        let family = u16::from_be_bytes(file.get(at..at + 2)?.try_into().ok()?);
        at += 2;
        let address = counted(file, &mut at)?;
        let display = counted(file, &mut at)?;
        let name = counted(file, &mut at)?;
        let data = counted(file, &mut at)?;
        let here = family == FAMILY_WILD || (family == FAMILY_LOCAL && address == host);
        if here && (display.is_empty() || display == number.as_bytes()) && name == COOKIE {
            return Some(data.to_vec());
        }
    }
    None
}

/// The Xauthority file libXau would read: `XAUTHORITY`, or `.Xauthority`
/// in the home directory.
fn authority_file() -> Option<PathBuf> {
    match std::env::var_os("XAUTHORITY").filter(|v| !v.is_empty()) {
        Some(f) => Some(PathBuf::from(f)),
        None => std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".Xauthority")),
    }
}

fn padded(request: &mut Vec<u8>, bytes: &[u8]) {
    request.extend_from_slice(bytes);
    request.resize(request.len().next_multiple_of(4), 0);
}

/// The connection setup, little-endian, protocol 11.0.
fn setup_request(cookie: Option<&[u8]>) -> Vec<u8> {
    let (name, data) = match cookie {
        Some(c) => (COOKIE, c),
        None => (&b""[..], &b""[..]),
    };
    let mut r = vec![b'l', 0];
    for v in [11, 0, name.len() as u16, data.len() as u16, 0] {
        r.extend_from_slice(&u16::to_le_bytes(v));
    }
    padded(&mut r, name);
    padded(&mut r, data);
    r
}

fn intern_atom_request(name: &str) -> Vec<u8> {
    let len = name.len();
    let mut r = vec![16, 1];
    r.extend_from_slice(&u16::to_le_bytes(
        ((8 + len.next_multiple_of(4)) / 4) as u16,
    ));
    r.extend_from_slice(&u16::to_le_bytes(len as u16));
    r.extend_from_slice(&[0, 0]);
    padded(&mut r, name.as_bytes());
    r
}

fn get_property_request(window: u32, atom: u32) -> Vec<u8> {
    let mut r = vec![20, 0];
    r.extend_from_slice(&u16::to_le_bytes(6));
    for v in [window, atom, ATOM_STRING, 0, MAX_VALUE] {
        r.extend_from_slice(&v.to_le_bytes());
    }
    r
}

fn u16_at(b: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_le_bytes(b.get(at..at + 2)?.try_into().ok()?))
}

fn u32_at(b: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_le_bytes(b.get(at..at + 4)?.try_into().ok()?))
}

/// The root window of `screen`, from the data of a setup reply that
/// accepted the connection.
fn root_window(setup: &[u8], screen: usize) -> Option<u32> {
    let vendor = usize::from(u16_at(setup, 16)?);
    let screens = usize::from(*setup.get(20)?);
    let formats = usize::from(*setup.get(21)?);
    if screen >= screens {
        return None;
    }
    let mut at = 32 + vendor.next_multiple_of(4) + 8 * formats;
    for _ in 0..screen {
        let depths = *setup.get(at + 39)?;
        at += 40;
        for _ in 0..depths {
            at += 8 + 24 * usize::from(u16_at(setup, at + 2)?);
        }
    }
    u32_at(setup, at)
}

fn malformed(what: &str) -> Error {
    Error::Refused(format!("the X server sent {what}"))
}

async fn send(stream: &UnixStream<Shared>, bytes: &[u8]) -> io::Result<()> {
    let mut at = 0;
    while at < bytes.len() {
        at += stream
            .write_with(|s| Ok(rustix::net::send(s, &bytes[at..], SendFlags::NOSIGNAL)?))
            .await?;
    }
    Ok(())
}

async fn receive(stream: &UnixStream<Shared>, buf: &mut [u8]) -> io::Result<()> {
    let mut at = 0;
    while at < buf.len() {
        let n = stream.read_with(|mut s| s.read(&mut buf[at..])).await?;
        if n == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        at += n;
    }
    Ok(())
}

/// The reply to request `sequence`, passing over events on the way. No
/// window of this connection asks for events, but MappingNotify goes to
/// every client.
async fn reply(stream: &UnixStream<Shared>, sequence: u16) -> Result<Vec<u8>, Error> {
    loop {
        let mut r = vec![0; 32];
        receive(stream, &mut r).await?;
        let more = || {
            u32_at(&r, 4)
                .and_then(|n| usize::try_from(n).ok())
                .and_then(|n| n.checked_mul(4))
                .filter(|n| *n <= MAX_REPLY)
                .ok_or_else(|| malformed("a reply too long to read"))
        };
        match r[0] {
            0 => {
                return Err(Error::Refused(format!(
                    "the X server answered request {sequence} with error {}",
                    r[1]
                )));
            }
            1 => {
                let more = more()?;
                r.resize(32 + more, 0);
                receive(stream, &mut r[32..]).await?;
                if u16_at(&r, 2) != Some(sequence) {
                    return Err(malformed("a reply out of order"));
                }
                return Ok(r);
            }
            // GenericEvent, which carries more than the usual 32 bytes.
            kind if kind & 0x7f == 35 => {
                let mut rest = vec![0; more()?];
                receive(stream, &mut rest).await?;
            }
            _ => {}
        }
    }
}

/// Sets up a connection over `stream` and reads `property` from the root
/// window of `screen`. None when the server has no such property, or holds
/// something other than a string there.
async fn exchange(
    stream: &UnixStream<Shared>,
    cookie: Option<&[u8]>,
    screen: usize,
    property: &str,
) -> Result<Option<String>, Error> {
    send(stream, &setup_request(cookie)).await?;
    let mut head = [0u8; 8];
    receive(stream, &mut head).await?;
    let mut setup = vec![0; usize::from(u16::from_le_bytes([head[6], head[7]])) * 4];
    receive(stream, &mut setup).await?;
    if head[0] != 1 {
        let reason = setup.get(..usize::from(head[1])).unwrap_or_default();
        return Err(Error::Refused(format!(
            "the X server refused the connection: {}",
            String::from_utf8_lossy(reason).trim_end()
        )));
    }
    let root = root_window(&setup, screen).ok_or_else(|| malformed("no such screen"))?;
    send(stream, &intern_atom_request(property)).await?;
    let atom = u32_at(&reply(stream, 1).await?, 8).unwrap_or(0);
    if atom == 0 {
        return Ok(None);
    }
    send(stream, &get_property_request(root, atom)).await?;
    let r = reply(stream, 2).await?;
    let len = u32_at(&r, 16).and_then(|n| usize::try_from(n).ok());
    let value = match (r[1], len) {
        (8, Some(n)) => r
            .get(32..32 + n)
            .ok_or_else(|| malformed("a short property"))?,
        _ => return Ok(None),
    };
    let value = String::from_utf8_lossy(value);
    let value = value.trim_end_matches('\0');
    Ok((!value.is_empty()).then(|| value.to_owned()))
}

/// The string `property` holds on the root window of `display`, or None when
/// it holds none. The display's abstract socket is tried first, then its
/// path, as libxcb does.
pub async fn root_property(display: Display, property: &str) -> Result<Option<String>, Error> {
    let runtime = SharedRuntime::current()?;
    let path = format!("/tmp/.X11-unix/X{}", display.number);
    let name = SocketAddr::from_abstract_name(path.as_bytes())?;
    let stream = match UnixStream::connect_addr(&runtime, &name).await {
        Ok(s) => s,
        Err(_) => UnixStream::connect(&runtime, &path).await?,
    };
    // The file can sit on a slow mount, so it is read off this thread.
    let file = match authority_file() {
        Some(f) => zruntime::unblock(move || std::fs::read(f)).await.ok(),
        None => None,
    };
    let host = rustix::system::uname();
    let cookie = file.and_then(|f| cookie(&f, host.nodename().to_bytes(), display.number));
    exchange(&stream, cookie.as_deref(), display.screen, property).await
}

#[cfg(test)]
mod tests {
    use std::os::unix::net::UnixStream as StdStream;
    use std::time::{Duration, Instant};

    use super::*;
    use crate::testing::returns;
    use crate::time;

    #[test]
    fn only_a_local_display_is_taken() {
        let d = |number, screen| Some(Display { number, screen });
        assert_eq!(Display::parse(":0"), d(0, 0));
        assert_eq!(Display::parse(":99"), d(99, 0));
        assert_eq!(Display::parse(":1.2"), d(1, 2));
        assert_eq!(Display::parse("unix:3"), d(3, 0));
        for other in [
            "localhost:10.0",
            "host::0",
            "/tmp/launch-x/org.x:0",
            ":",
            ":x",
            ":1.",
            ":+1",
            "",
        ] {
            assert_eq!(Display::parse(other), None, "{other}");
        }
    }

    fn entry(family: u16, address: &str, display: &str, name: &[u8], data: &[u8]) -> Vec<u8> {
        let mut e = family.to_be_bytes().to_vec();
        for f in [address.as_bytes(), display.as_bytes(), name, data] {
            e.extend_from_slice(&(f.len() as u16).to_be_bytes());
            e.extend_from_slice(f);
        }
        e
    }

    #[test]
    fn the_cookie_is_the_first_for_this_host_and_display() {
        let file = [
            entry(FAMILY_LOCAL, "elsewhere", "1", COOKIE, b"A"),
            entry(FAMILY_LOCAL, "here", "0", b"XDM-AUTHORIZATION-1", b"B"),
            entry(FAMILY_LOCAL, "here", "1", COOKIE, b"C"),
            entry(FAMILY_WILD, "", "0", COOKIE, b"D"),
            entry(FAMILY_LOCAL, "here", "", COOKIE, b"E"),
        ]
        .concat();
        assert_eq!(cookie(&file, b"here", 1).as_deref(), Some(&b"C"[..]));
        assert_eq!(cookie(&file, b"here", 0).as_deref(), Some(&b"D"[..]));
        assert_eq!(cookie(&file, b"here", 7).as_deref(), Some(&b"E"[..]));
        assert_eq!(cookie(&file, b"nowhere", 7), None);
        assert_eq!(cookie(&file[..file.len() - 1], b"nowhere", 7), None);
    }

    /// A setup reply with two screens, the first with one depth of one
    /// visual, so that finding the second's root skips them.
    fn setup_reply() -> Vec<u8> {
        let mut data = vec![0u8; 32];
        data[16..18].copy_from_slice(&5u16.to_le_bytes());
        data[20] = 2;
        data[21] = 1;
        padded(&mut data, b"Xvfb!");
        data.extend_from_slice(&[0; 8]);
        let mut first = vec![0u8; 40];
        first[0..4].copy_from_slice(&0x111u32.to_le_bytes());
        first[39] = 1;
        data.extend_from_slice(&first);
        let mut depth = vec![24, 0];
        depth.extend_from_slice(&1u16.to_le_bytes());
        depth.extend_from_slice(&[0; 4 + 24]);
        data.extend_from_slice(&depth);
        let mut second = vec![0u8; 40];
        second[0..4].copy_from_slice(&0x222u32.to_le_bytes());
        data.extend_from_slice(&second);
        let mut r = vec![1, 0];
        r.extend_from_slice(&11u16.to_le_bytes());
        r.extend_from_slice(&0u16.to_le_bytes());
        r.extend_from_slice(&((data.len() / 4) as u16).to_le_bytes());
        r.extend_from_slice(&data);
        r
    }

    fn read_n(s: &mut StdStream, n: usize) -> Vec<u8> {
        let mut b = vec![0; n];
        s.read_exact(&mut b).unwrap();
        b
    }

    /// A server that checks each request it is sent and answers it.
    fn server(mut s: StdStream) {
        use std::io::Write;
        let cookie = b"secret-cookie-16";
        let head = read_n(&mut s, 12);
        assert_eq!(&head[..6], &[b'l', 0, 11, 0, 0, 0]);
        assert_eq!(u16_at(&head, 6), Some(COOKIE.len() as u16));
        assert_eq!(u16_at(&head, 8), Some(cookie.len() as u16));
        let auth = read_n(&mut s, 20 + 16);
        assert_eq!(&auth[..18], COOKIE);
        assert_eq!(&auth[20..], cookie);
        s.write_all(&setup_reply()).unwrap();
        let intern = read_n(&mut s, 8 + 12);
        assert_eq!(&intern[..8], &[16, 1, 5, 0, 10, 0, 0, 0]);
        assert_eq!(&intern[8..18], b"AT_SPI_BUS");
        let mut atom = vec![1, 0, 1, 0, 0, 0, 0, 0];
        atom.extend_from_slice(&77u32.to_le_bytes());
        atom.resize(32, 0);
        // An event no one asked for, and a GenericEvent with more after it.
        let mut mapping = vec![34];
        mapping.resize(32, 0);
        let mut generic = vec![35, 0, 0, 0, 1, 0, 0, 0];
        generic.resize(36, 0);
        s.write_all(&[mapping, generic, atom].concat()).unwrap();
        let get = read_n(&mut s, 24);
        assert_eq!(u32_at(&get, 4), Some(0x222));
        assert_eq!(u32_at(&get, 8), Some(77));
        assert_eq!(u32_at(&get, 12), Some(ATOM_STRING));
        let value = b"unix:path=/run/a11y,guid=1\0";
        let mut r = vec![1, 8, 2, 0];
        r.extend_from_slice(&(value.len().div_ceil(4) as u32).to_le_bytes());
        r.extend_from_slice(&ATOM_STRING.to_le_bytes());
        r.extend_from_slice(&0u32.to_le_bytes());
        r.extend_from_slice(&(value.len() as u32).to_le_bytes());
        r.resize(32, 0);
        padded(&mut r, value);
        s.write_all(&r).unwrap();
    }

    fn pair() -> (UnixStream<Shared>, StdStream) {
        let (ours, theirs) = StdStream::pair().unwrap();
        ours.set_nonblocking(true).unwrap();
        let runtime = SharedRuntime::current().unwrap();
        (UnixStream::from_std(&runtime, ours).unwrap(), theirs)
    }

    #[test]
    fn the_property_is_read_from_the_screens_root_window() {
        let value = returns(|| {
            zbus::block_on(async {
                let (ours, theirs) = pair();
                let serving = std::thread::spawn(move || server(theirs));
                let value = exchange(&ours, Some(b"secret-cookie-16"), 1, "AT_SPI_BUS").await;
                serving.join().unwrap();
                value.unwrap()
            })
        });
        assert_eq!(value.as_deref(), Some("unix:path=/run/a11y,guid=1"));
    }

    #[test]
    fn a_server_that_never_answers_is_given_up_on_at_the_deadline() {
        let (r, waited) = returns(|| {
            zbus::block_on(async {
                let (ours, _theirs) = pair();
                let t = Instant::now();
                let r = time::within(
                    Duration::from_millis(200),
                    "reading the root window",
                    exchange(&ours, None, 0, "AT_SPI_BUS"),
                )
                .await;
                (r.map(|_| ()), t.elapsed())
            })
        });
        assert!(matches!(r, Err(Error::TimedOut(_))), "{r:?}");
        assert!(waited < Duration::from_secs(2), "{waited:?}");
    }

    #[test]
    fn a_refusal_says_why() {
        let r = returns(|| {
            zbus::block_on(async {
                let (ours, mut theirs) = pair();
                let serving = std::thread::spawn(move || {
                    use std::io::Write;
                    read_n(&mut theirs, 12);
                    let reason = b"No protocol specified\n";
                    let mut r = vec![0, reason.len() as u8, 11, 0, 0, 0];
                    r.extend_from_slice(&(reason.len().div_ceil(4) as u16).to_le_bytes());
                    padded(&mut r, reason);
                    theirs.write_all(&r).unwrap();
                });
                let r = exchange(&ours, None, 0, "AT_SPI_BUS").await;
                serving.join().unwrap();
                r.map_err(|e| e.to_string())
            })
        });
        assert_eq!(
            r,
            Err("refused: the X server refused the connection: No protocol specified".into())
        );
    }
}
