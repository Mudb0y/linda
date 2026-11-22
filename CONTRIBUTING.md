# Contributing to Linda

Issues and pull requests are both welcome, large ones included.

## Reporting a problem

Say what you did, what Linda said, and what you expected. It helps to name:

* your distribution and desktop, and whether the session is X11 or Wayland;
* the commit of Linda you run;
* the program, and its version if it matters.

A log helps too. It is at `~/.local/state/linda/nvda.log`.

**Read a log before you share it.** At the default logging level it holds neither what you typed nor what Linda said. With the level set to "input/output" or "debug", in the General settings or with `--debug-logging`, it records everything Linda speaks, as NVDA does on Windows. That includes each character you type, because typing echo is speech, and everything Linda reads to you: pages, messages, file names. Passwords typed into password fields are spoken as "star" and are not in it. A debug log is often what finds a fault, so it is welcome, but look through it first, or send it privately.

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
* Linda's own code writes nothing a person types to a log, at any level: no key names, key codes or characters. What NVDA's speech log records at the debug levels is the one exception, and it is left as NVDA has it.
* Main is what people run as their screen reader, so every change goes through the same checks before it lands.

Linda is distributed under NVDA's licence, in [copying.txt](./copying.txt), and so is a change you send.
