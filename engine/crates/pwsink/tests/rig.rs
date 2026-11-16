//! libpwsink with no daemon, inside tests/linux/rig only, where initialising PipeWire cannot reach
//! the live sound server. Outside, in ROOT/engine:
//!
//!     nix develop ROOT -c cargo test --release -p basedreader-pwsink --features rig --no-run
//!
//! then the test binary it names, inside: ROOT/tests/linux/rig ROOT/engine/target/release/deps/rig-...

use pwsink::{
    pwsink_close, pwsink_connected, pwsink_delivered, pwsink_init, pwsink_open, pwsink_played,
    pwsink_start, pwsink_write,
};

/// Refuses to go on outside the rig's sandbox, by the same signs tests/linux/harness.py checks.
fn require_rig() {
    let first = std::fs::read_to_string("/proc/1/comm").unwrap_or_default();
    assert!(
        std::env::var_os("BASEDREADER_RIG").is_some_and(|v| v == "1")
            && first.trim() == "bwrap"
            && !std::path::Path::new("/run/user").exists(),
        "refusing to initialise PipeWire outside tests/linux/rig"
    );
}

#[test]
fn with_no_daemon_a_stream_opens_and_keeps_time() {
    require_rig();
    assert_eq!(pwsink_init(), 0);
    let mut err = 0;
    let s = unsafe { pwsink_open(1, 1, 8000, 16, std::ptr::null(), &mut err) };
    assert!(!s.is_null(), "{err}");
    let data = vec![0u8; 1600 * 2];
    let started = std::time::Instant::now();
    assert_eq!(unsafe { pwsink_write(s, data.as_ptr(), data.len()) }, 1600);
    unsafe { pwsink_start(s) };
    while unsafe { pwsink_played(s) } < 1600 {
        assert!(started.elapsed().as_millis() < 1000);
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    let took = started.elapsed().as_millis();
    assert!((195..400).contains(&took), "{took} ms for 200 ms of audio");
    assert_eq!(pwsink_delivered(), 0);
    assert_eq!(pwsink_connected(), 0);
    unsafe { pwsink_close(s) };
}
