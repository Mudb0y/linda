# AGENTS.md

Guidance for AI coding agents working on **Linda** (NVDA 2026.3.1 ported to Linux).

## Core Rules

1. **Keep upstream NVDA diffs minimal (`tools/check-upstream-edits`).**
   - Put new Linux code under `source/linuxPlatform/` or `source/NVDAObjects/AtSpi/`.
   - Any edit to an upstream NVDA Python file must guard Linux-only behavior with `if sys.platform != "win32":` (not `wx.Platform` or other checks, because `tools/check-upstream-edits` evaluates `sys.platform` statically to verify Windows AST parity) and add a matching entry to `tools/upstream-edits.txt`.
   - Do not place Linux-only tests under `tests/unit/` without registering them; put Linux tests under `tests/linux/`.
   - Upstream modules replaced via `source/linuxPlatform/finder.py` are tracked in `tools/upstream-forks.txt`.
   - Always run `nix develop . -c python3 tools/check-upstream-edits` before committing.

2. **Never log user keystrokes.**
   - Linda's own code must never log keys pressed, key names, key codes, or typed characters at any log level (including debug). NVDA's own speech debug logging is the only exception and is left as upstream has it.

3. **Formatting and linting.**
   - Follow NVDA's Python style: tabs for indentation, `ruff format` and `ruff check` clean (verified by `tests/linux/test_ruff.py`, line length 110 per `pyproject.toml`).

4. **Verify changes with tests.**
   - Add regression tests under `tests/linux/` (or Rust unit tests in `engine/crates/vbufengine/`) whenever possible.

## Repository Layout

- `source/linda.py`: Linux entry point (counterpart to `source/nvda.pyw`).
- `source/linuxPlatform/`: Linux platform layer — AT-SPI client/handler/roles (`atspi/`), X11/Wayland/kernel key capture (`keys/`), `vbufEngine.py`, `vbufLib.py`, `vbufUpdates.py`, `processes.py`, `inputHook.py`, and upstream module replacements (`replacements/`).
- `source/NVDAObjects/AtSpi/`: AT-SPI NVDAObject layer (`__init__.py`, `textInfo.py`, `compoundText.py`, `terminal.py`, `deletion.py`).
- `source/virtualBuffers/atspi.py`: Browse mode `AtSpiBuffer` and `AtSpiBufferTextInfo`.
- `engine/crates/vbufengine/`: Rust AT-SPI virtual buffer renderer and watcher (`libbre_vbuf.so` — `engine.rs`, `walk.rs`, `render.rs`, `lib.rs`).
- `engine/vbuf/`: C++ VBuf storage library (`vbufLinux.cpp`).
- `linux/`: `linda-keyd` kernel key-capture helper daemon and packaging specs.
- `tests/linux/`: Sandboxed desktop, browser (Firefox/Chromium), terminal, and Wayland rig tests, plus fixtures in `tests/linux/fixtures/`.
- `tools/`: `check-upstream-edits`, `upstream-edits.txt`, `upstream-forks.txt`.

## Build and Test Commands

All tests run inside a sandboxed rig (`tests/linux/rig`) so they do not disturb the host desktop or screen reader.

### Upstream-edit and Lint Checks
```bash
nix develop . -c python3 tools/check-upstream-edits
nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_ruff.py
```

### Rust `vbufengine` Build and Tests
`.cargo/config.toml` sets `runner = "./run-in-rig"`, so run `cargo test` from inside `engine/`:
```bash
nix develop . -c cargo build --release --offline --manifest-path engine/Cargo.toml -p linda-vbufengine
(cd engine && nix develop .. -c cargo test --offline -p linda-vbufengine)
```

### Linux Rig Tests (X11 and Wayland)
- For X11 desktop, terminal, and browser tests, set `LINDA_RIG_WM=marco`:
  ```bash
  LINDA_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_atspiObjects.py
  ```
- **Testing local `vbufengine` changes:** `flake.nix` defaults `LINDA_VBUF_ENGINE` to the Nix-built `libbre_vbuf.so` unless already set in the environment. When testing uncommitted Rust edits, point `LINDA_VBUF_ENGINE` to your local release build inside the repo tree (so `tests/linux/rig` mounts it):
  ```bash
  LINDA_VBUF_ENGINE="$PWD/engine/target/release/libbre_vbuf.so" LINDA_RIG_WM=marco \
    nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_chromium.py
  ```
- For Wayland rig tests (e.g., sway):
  ```bash
  LINDA_RIG_COMPOSITOR=sway nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_waylandProxyApp.py
  ```
- Run heavy browser and desktop rig suites **sequentially** rather than concurrently to avoid CPU contention on timing assertions (such as large-page load bounds).

### Common Targeted Suites
- AT-SPI desktop controls (GTK/Qt/wx): `tests/linux/test_atspiObjects.py`, `tests/linux/test_usable.py`
- NVDA's own wxGTK dialogs/menus: `tests/linux/test_ownGui.py`
- Browse Mode (Firefox / Chromium / engine): `tests/linux/test_browseUsable.py`, `tests/linux/test_browseMode.py`, `tests/linux/test_browseFeatures.py`, `tests/linux/test_chromium.py`, `tests/linux/test_vbufEngine.py`
- MATE Terminal / VTE: `tests/linux/test_atspiTerminal.py`, `tests/linux/test_terminalApps.py`, `tests/linux/test_terminalWords.py`
- Key capture / process identity: `tests/linux/test_kernelKeys.py`, `tests/linux/test_processIdentity.py`, `tests/linux/test_waylandProxyApp.py`
