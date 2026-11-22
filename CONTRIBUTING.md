# Contributing to Linda

Issues and pull requests are both welcome, large ones included.

## Reporting a problem

Say what you did, what Linda said, and what you expected. It helps to name:

* your distribution and desktop, and whether the session is X11 or Wayland;
* the commit of Linda you run;
* the program, and its version if it matters.

A log helps too. It is at `~/.local/state/linda/nvda.log`. A log holds what Linda spoke, which includes what you typed and read, so look through it before you attach it.

## Testing

Reports from real machines are worth a lot, from Wayland sessions above all: GNOME 49 and later, KDE Plasma 6.5 and later and sway have so far been tested only in virtual machines.

## Sending a change

* The project moves fast. Rebase on main just before you open a pull request, and check that main does not already fix what you are fixing.
* For something large, open an issue first, so that two people do not build the same thing.
* Linda is NVDA with as few changes to NVDA's own files as possible, so that NVDA's releases can still be merged. A change to one of NVDA's files goes inside an `if sys.platform != "win32":` branch and gets an entry in `tools/upstream-edits.txt`, which `tools/check-upstream-edits` checks. New Linux code goes under `source/linuxPlatform/`.
* Include a test where one is possible. Tests run inside a sandbox, so they cannot disturb your own desktop or screen reader:

  ```
  nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_X.py
  ```

* Follow NVDA's style: tabs for indentation, and `ruff format .` and `ruff check .` clean.
* Nothing a person types may be written to a log, at any level.
* Main is what people run as their screen reader, so every change goes through the same checks before it lands.

Linda is distributed under NVDA's licence, in [copying.txt](./copying.txt), and so is a change you send.
