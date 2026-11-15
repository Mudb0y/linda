//! CPU time from /proc and for this process.

use std::collections::HashMap;
use std::time::Instant;

use mirror::client::Counters;
use serde::Serialize;

fn clock_ticks() -> f64 {
    // SAFETY: sysconf has no preconditions.
    let t = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    if t > 0 { t as f64 } else { 100.0 }
}

/// utime + stime of every thread of `pid`, living or exited, in seconds.
pub fn proc_cpu(pid: u32) -> Option<f64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let after = stat.rsplit_once(')')?.1;
    let f: Vec<&str> = after.split_whitespace().collect();
    // Fields 14 and 15 of stat(5); `after` starts at field 3.
    let utime: f64 = f.get(11)?.parse().ok()?;
    let stime: f64 = f.get(12)?.parse().ok()?;
    Some((utime + stime) / clock_ticks())
}

/// Time on CPU of one thread from its schedstat, in nanoseconds. Unlike
/// /proc/PID/stat, which counts in clock ticks of 10 ms, this resolves the
/// cost of a single short call.
pub fn thread_cpu_ns(pid: u32, tid: u32) -> Option<u64> {
    let s = std::fs::read_to_string(format!("/proc/{pid}/task/{tid}/schedstat")).ok()?;
    s.split_whitespace().next()?.parse().ok()
}

/// Every live thread of `pid` with its schedstat CPU time.
fn threads_cpu_ns(pid: u32) -> HashMap<u32, u64> {
    let Ok(dir) = std::fs::read_dir(format!("/proc/{pid}/task")) else {
        return HashMap::new();
    };
    dir.flatten()
        .filter_map(|e| e.file_name().to_str()?.parse::<u32>().ok())
        .filter_map(|tid| Some((tid, thread_cpu_ns(pid, tid)?)))
        .collect()
}

fn ppid(pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat.rsplit_once(')')?
        .1
        .split_whitespace()
        .nth(1)?
        .parse()
        .ok()
}

pub fn comm(pid: u32) -> String {
    std::fs::read_to_string(format!("/proc/{pid}/comm"))
        .map(|s| s.trim().to_owned())
        .unwrap_or_default()
}

/// `pid` and every process descended from it.
pub fn process_tree(pid: u32) -> Vec<u32> {
    let mut parents: HashMap<u32, Vec<u32>> = HashMap::new();
    if let Ok(dir) = std::fs::read_dir("/proc") {
        for e in dir.flatten() {
            let Some(p) = e.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else {
                continue;
            };
            if let Some(pp) = ppid(p) {
                parents.entry(pp).or_default().push(p);
            }
        }
    }
    let mut out = vec![pid];
    let mut i = 0;
    while i < out.len() {
        if let Some(kids) = parents.get(&out[i]) {
            out.extend(kids);
        }
        i += 1;
    }
    out
}

/// CPU time of this whole process, all threads, in seconds.
pub fn self_cpu() -> f64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: ts is a valid timespec for the call to fill.
    unsafe { libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut ts) };
    ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9
}

#[derive(Debug, Clone, Copy, Default, Serialize)]
pub struct Cost {
    pub wall_s: f64,
    pub client_cpu_s: f64,
    /// The process that owns the application's bus name, from /proc/PID/stat
    /// in 10 ms clock ticks, threads that exited meanwhile included.
    pub app_cpu_s: f64,
    /// The same process from per-thread schedstat in nanoseconds: threads
    /// alive at the end, counted from the start or from their creation.
    pub app_threads_cpu_s: f64,
    /// Its main thread alone, which is where the ATK bridge serves every
    /// call.
    pub app_main_thread_cpu_s: f64,
    /// That process and every process descended from it.
    pub app_tree_cpu_s: f64,
    pub calls: u64,
    pub reply_bytes: u64,
    pub errors: u64,
    pub timeouts: u64,
}

pub struct Meter {
    pid: u32,
    wall: Instant,
    client: f64,
    tree: HashMap<u32, f64>,
    threads: HashMap<u32, u64>,
    counters: Counters,
}

impl Meter {
    pub fn start(pid: u32, counters: Counters) -> Self {
        let tree = process_tree(pid)
            .into_iter()
            .filter_map(|p| Some((p, proc_cpu(p)?)))
            .collect();
        let threads = threads_cpu_ns(pid);
        Meter {
            pid,
            wall: Instant::now(),
            client: self_cpu(),
            tree,
            threads,
            counters,
        }
    }

    /// Processes that exited in between drop out of the tree figure, and ones
    /// started in between are not counted.
    pub fn stop(&self, counters: Counters) -> Cost {
        let wall = self.wall.elapsed().as_secs_f64();
        let client = self_cpu() - self.client;
        let mut owner = 0.0;
        let mut tree = 0.0;
        for (&p, &before) in &self.tree {
            if let Some(now) = proc_cpu(p) {
                let d = (now - before).max(0.0);
                tree += d;
                if p == self.pid {
                    owner = d;
                }
            }
        }
        let mut threads_ns = 0u64;
        let mut main_ns = 0u64;
        for (tid, now) in threads_cpu_ns(self.pid) {
            let d = now.saturating_sub(self.threads.get(&tid).copied().unwrap_or(0));
            threads_ns += d;
            if tid == self.pid {
                main_ns = d;
            }
        }
        let c = counters - self.counters;
        Cost {
            wall_s: round(wall),
            client_cpu_s: round(client),
            app_cpu_s: round(owner),
            app_threads_cpu_s: round(threads_ns as f64 * 1e-9),
            app_main_thread_cpu_s: round(main_ns as f64 * 1e-9),
            app_tree_cpu_s: round(tree),
            calls: c.calls,
            reply_bytes: c.reply_bytes,
            errors: c.errors,
            timeouts: c.timeouts,
        }
    }
}

fn round(x: f64) -> f64 {
    (x * 1e6).round() / 1e6
}
