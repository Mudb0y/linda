//! Times fetching an application's whole accessibility tree, and keeping a
//! mirror of it current. Runs only inside tests/linux/rig.
//!
//! linda-bench wait   --title T [--mode cold|warm] [--settle-ms N]
//!                          [--app NAME] [--timeout-s N]
//! linda-bench fetch  --title T --page NAME --out FILE [--run-index N]
//!                          [--seed N] [--wait-info FILE]
//!                          [--methods LABEL,...|none]
//!                          [--app NAME] [--method-timeout-ms N]
//!                          [--connect-timeout-ms N]
//! linda-bench mirror --title T --out FILE --watch-ms N [--run-index N]
//!                          [--seed N] [--interval-ms N] [--wait-info FILE]
//!                          [--coalesce-ms N] [--dump FILE]
//!                          [--app NAME] [--method-timeout-ms N]
//!                          [--connect-timeout-ms N]

mod fetchcmd;
mod mirrorcmd;
mod procstat;

use std::collections::HashMap;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use mirror::client::Client;
use mirror::discovery::{self, BusAddress, Peer, Timeouts};
use mirror::fetch::{self, WalkFields, WalkLimits};
use mirror::{Error, app, consts, time};
use serde::Serialize;
use zbus::Connection;

/// Generous enough for a sequential walk of the large page over the bus,
/// which takes about a minute and a half.
pub const WALK_LIMITS: WalkLimits = WalkLimits {
    max_objects: 1_000_000,
    max_time: Duration::from_secs(600),
};

pub async fn sleep(d: Duration) -> Result<(), String> {
    time::sleep(d).map_err(|e| format!("no timer: {e}"))?.await;
    Ok(())
}

pub struct Args(HashMap<String, String>);

impl Args {
    fn parse(it: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut m = HashMap::new();
        let mut it = it.peekable();
        while let Some(k) = it.next() {
            let Some(k) = k.strip_prefix("--") else {
                return Err(format!("unexpected argument {k}"));
            };
            let v = it.next().ok_or_else(|| format!("--{k} needs a value"))?;
            m.insert(k.to_owned(), v);
        }
        Ok(Args(m))
    }

    pub fn get(&self, k: &str) -> Result<&str, String> {
        self.0
            .get(k)
            .map(String::as_str)
            .ok_or_else(|| format!("--{k} is required"))
    }

    pub fn or<'a>(&'a self, k: &str, default: &'a str) -> &'a str {
        self.0.get(k).map_or(default, String::as_str)
    }

    pub fn num(&self, k: &str, default: u64) -> Result<u64, String> {
        self.0.get(k).map_or(Ok(default), |v| {
            v.parse().map_err(|_| format!("--{k}: not a number"))
        })
    }
}

/// Everything about the application under test that every subcommand needs.
pub struct Target {
    pub bus: Connection,
    pub bus_address: BusAddress,
    pub app_bus: String,
    pub pid: u32,
    pub owner: Peer,
    pub timeouts: Timeouts,
}

#[derive(Serialize)]
pub struct TargetInfo {
    pub bus_discovery: discovery::Source,
    pub bus_addresses_refused: Vec<String>,
    pub app_bus_name: String,
    pub pid: u32,
    pub process_name: String,
    pub counted_processes: Vec<(u32, String)>,
    pub toolkit: String,
    pub toolkit_version: String,
    pub document: Option<String>,
    pub method_timeout_ms: u128,
    pub connect_timeout_ms: u128,
}

pub async fn connect(args: &Args) -> Result<Target, String> {
    let timeouts = Timeouts {
        method: Duration::from_millis(args.num("method-timeout-ms", 30_000)?),
        connect: Duration::from_millis(args.num("connect-timeout-ms", 10_000)?),
    };
    let bus_address = discovery::find_bus_address(timeouts)
        .await
        .map_err(|e| format!("finding the accessibility bus: {e}"))?;
    let bus = discovery::connect_bus(&bus_address.address, timeouts)
        .await
        .map_err(|e| format!("connecting to {}: {e}", bus_address.address))?;
    let want = args.or("app", "Firefox");
    let timeout = Duration::from_secs(args.num("timeout-s", 120)?);
    let app_bus = app::find_app(&bus, want, timeout)
        .await
        .map_err(|e| e.to_string())?;
    let owner = app::owner(&bus, &app_bus)
        .await
        .map_err(|e| format!("owner of {app_bus}: {e}"))?;
    Ok(Target {
        bus,
        bus_address,
        app_bus,
        pid: owner.pid,
        owner,
        timeouts,
    })
}

/// How a run got its connection for calls: peer to peer when the
/// application offers a server the engine accepts and the connection is
/// made in time, otherwise over the bus.
#[derive(Serialize, Default)]
pub struct PeerToPeer {
    pub address_offered: bool,
    pub connect_s: Option<f64>,
    /// Why the run fell back to the bus, when it did.
    pub fell_back: Option<String>,
}

impl Target {
    pub fn bus_client(&self) -> Client {
        Client::bus(self.bus.clone(), &self.app_bus)
    }

    /// A peer-to-peer client to the server at `address`, or the reason there
    /// is none.
    pub async fn p2p_client(&self, address: &str, p2p: &mut PeerToPeer) -> Option<Client> {
        p2p.address_offered = !address.is_empty();
        if address.is_empty() {
            return None;
        }
        let t = Instant::now();
        match discovery::connect_p2p(address, self.owner, self.timeouts).await {
            Ok(conn) => {
                p2p.connect_s = Some(t.elapsed().as_secs_f64());
                Some(Client::p2p(conn, &self.app_bus))
            }
            Err(e) => {
                eprintln!("linda-bench: using the bus, peer to peer failed: {e}");
                p2p.fell_back = Some(e.to_string());
                None
            }
        }
    }

    pub async fn info(&self, title: &str) -> TargetInfo {
        let c = self.bus_client();
        let prop = |name: &'static str| {
            let c = &c;
            async move {
                app::property_string(c, consts::ROOT_PATH, consts::IFACE_APPLICATION, name)
                    .await
                    .unwrap_or_default()
            }
        };
        TargetInfo {
            bus_discovery: self.bus_address.source,
            bus_addresses_refused: self.bus_address.refused.clone(),
            app_bus_name: self.app_bus.clone(),
            pid: self.pid,
            process_name: procstat::comm(self.pid),
            counted_processes: procstat::process_tree(self.pid)
                .into_iter()
                .map(|p| (p, procstat::comm(p)))
                .collect(),
            toolkit: prop("ToolkitName").await,
            toolkit_version: prop("Version").await,
            document: app::find_document(&c, title).await.ok().flatten(),
            method_timeout_ms: self.timeouts.method.as_millis(),
            connect_timeout_ms: self.timeouts.connect.as_millis(),
        }
    }
}

pub fn write_json<T: Serialize>(path: &str, value: &T) -> Result<(), String> {
    let f = std::fs::File::create(path).map_err(|e| format!("{path}: {e}"))?;
    serde_json::to_writer_pretty(std::io::BufWriter::new(f), value)
        .map_err(|e| format!("{path}: {e}"))
}

/// What `wait` printed for this run, when the run script hands it over.
pub fn wait_info(args: &Args) -> serde_json::Value {
    args.0
        .get("wait-info")
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::Value::Null)
}

pub fn unix_time() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

/// Sums the CPU of `pid` and its descendants over `window`, in seconds.
async fn tree_cpu_over(pid: u32, window: Duration) -> Result<f64, String> {
    let sum = || -> f64 {
        procstat::process_tree(pid)
            .into_iter()
            .filter_map(procstat::proc_cpu)
            .sum()
    };
    let before = sum();
    sleep(window).await?;
    Ok(sum() - before)
}

/// Waits until the document titled `--title` exists and is not busy, then
/// until the page has settled, without activating the bridge.
///
/// With `--mode cold`, the default, the document is never walked: walking it
/// makes the application create an accessible object for every node, which
/// is the work that activation otherwise pays for. The page counts as settled
/// once `--settle-ms` has passed and the application's processes have then
/// been idle for two half-second windows in a row.
///
/// With `--mode warm` the document is walked over the bus until its object
/// count stops changing, so every object exists before activation.
async fn wait(args: &Args) -> Result<(), String> {
    let t0 = Instant::now();
    let title = args.get("title")?;
    let mode = args.or("mode", "cold");
    if mode != "cold" && mode != "warm" {
        return Err(format!("--mode {mode}: not cold or warm"));
    }
    let target = connect(args).await?;
    let t_app = t0.elapsed().as_secs_f64();
    let c = target.bus_client();
    let deadline = t0 + Duration::from_secs(args.num("timeout-s", 120)?);
    let doc = loop {
        if let Ok(Some(doc)) = app::find_document(&c, title).await {
            let busy = c
                .call(&doc, consts::IFACE_ACCESSIBLE, "GetState", &())
                .await
                .ok()
                .and_then(|m| mirror::decode::states(&m).ok())
                .is_none_or(|s| consts::has_state(s, consts::STATE_BUSY));
            if !busy {
                break doc;
            }
        }
        if Instant::now() > deadline {
            return Err(format!("no loaded document titled {title:?}"));
        }
        sleep(Duration::from_millis(250)).await?;
    };
    let t_doc = t0.elapsed().as_secs_f64();
    let mut document_objects = None;
    let mut quiet = None;
    if mode == "warm" {
        let mut last = 0;
        let mut stable = 0;
        while stable < 2 && Instant::now() < deadline {
            let n = fetch::walk(&c, &doc, 64, WalkFields::STRUCTURE, None, WALK_LIMITS)
                .await
                .tree
                .len();
            if n == last {
                stable += 1;
            } else {
                stable = 0;
                last = n;
            }
            sleep(Duration::from_millis(500)).await?;
        }
        document_objects = Some(last);
    } else {
        sleep(Duration::from_millis(args.num("settle-ms", 3000)?)).await?;
        let window = Duration::from_millis(500);
        let mut idle = 0;
        while idle < 2 && Instant::now() < deadline {
            if tree_cpu_over(target.pid, window).await? <= 0.02 {
                idle += 1;
            } else {
                idle = 0;
            }
        }
        quiet = Some(idle >= 2);
    }
    println!(
        "{}",
        serde_json::json!({
            "mode": mode,
            "app_bus_name": target.app_bus,
            "pid": target.pid,
            "document": doc,
            "t_app_s": t_app,
            "t_document_s": t_doc,
            "t_settled_s": t0.elapsed().as_secs_f64(),
            "quiet": quiet,
            "document_objects": document_objects,
        })
    );
    Ok(())
}

/// The bench finds the accessibility bus the way a screen reader does, so
/// anywhere but the rig it would walk the live desktop's applications. The
/// variable alone could be copied into another shell; the rig's sandbox is
/// also the first process and has no /run/user, as the rig itself checks.
fn inside_rig() -> bool {
    std::env::var("LINDA_RIG").as_deref() == Ok("1")
        && std::fs::read_to_string("/proc/1/comm").is_ok_and(|c| c.trim_end() == "bwrap")
        && !std::path::Path::new("/run/user").exists()
}

fn main() -> ExitCode {
    if !inside_rig() {
        eprintln!("linda-bench: runs only inside tests/linux/rig");
        return ExitCode::from(2);
    }
    let mut argv = std::env::args().skip(1);
    let cmd = argv.next().unwrap_or_default();
    let args = match Args::parse(argv) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("linda-bench: {e}");
            return ExitCode::from(2);
        }
    };
    let r = zbus::block_on(async {
        match cmd.as_str() {
            "wait" => wait(&args).await,
            "fetch" => fetchcmd::run(&args).await,
            "mirror" => mirrorcmd::run(&args).await,
            _ => Err("usage: linda-bench wait|fetch|mirror --title T ...".to_owned()),
        }
    });
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("linda-bench {cmd}: {e}");
            ExitCode::FAILURE
        }
    }
}

pub fn err(e: Error) -> String {
    e.to_string()
}
