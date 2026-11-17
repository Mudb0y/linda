//! The bench must refuse to start anywhere but inside the rig. Each case
//! names a command the bench does not know, so that even without the guard
//! it would only print its usage and touch no bus.

use std::process::Command;

fn run(rig_variable: Option<&str>) -> std::process::Output {
    let mut c = Command::new(env!("CARGO_BIN_EXE_linda-bench"));
    c.arg("no-such-command");
    match rig_variable {
        Some(v) => c.env("LINDA_RIG", v),
        None => c.env_remove("LINDA_RIG"),
    };
    c.output().expect("the bench runs")
}

fn in_sandbox() -> bool {
    std::fs::read_to_string("/proc/1/comm").is_ok_and(|c| c.trim_end() == "bwrap")
        && !std::path::Path::new("/run/user").exists()
}

#[test]
fn refuses_without_the_rig_variable() {
    let out = run(None);
    assert_eq!(out.status.code(), Some(2));
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
    assert!(stderr.contains("tests/linux/rig"), "{stderr}");
    assert!(out.stdout.is_empty());
}

#[test]
fn the_variable_alone_is_not_enough() {
    let out = run(Some("1"));
    // Inside the rig the guard lets it through to the unknown command.
    let expected = if in_sandbox() { 1 } else { 2 };
    assert_eq!(out.status.code(), Some(expected));
}
