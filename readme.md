# Linda

Linda Is Non-visual Desktop Access: a screen reader for the Linux desktop.

Linda is NVDA, the screen reader most blind people use on Windows, brought to Linux.
The commands, the settings, browse mode for web pages and the add-on interface are NVDA's own, so what you know from NVDA works here the way you expect.
Underneath, Linda reads programs through AT-SPI, speaks through PipeWire and takes its keys from X11.

Linda is based on NVDA 2026.3.1 by NV Access and the NVDA contributors.
It is an independent project and is not affiliated with or endorsed by NV Access.
Please report problems with Linda [here](https://github.com/Mudb0y/linda/issues), never to NV Access.

## Where it stands

Linda is young and changes every day.
Its author uses it as their only screen reader, so the daily paths work, and plenty of corners are still rough.

What works today:

* Speech with eSpeak NG and with [openevv](https://github.com/Mudb0y/openevv). Both come with Linda.
* GTK and Qt programs, the MATE desktop, terminals.
* Firefox, Chromium and Electron programs such as Discord, in browse mode: single letter navigation, the elements list, find, tables, forms and focus mode, live regions, and in Firefox a change of voice when the page changes language.
* NVDA's keyboard commands in the desktop and laptop layouts, with Insert, Caps Lock or both as the modifier key, which Linda calls the Linda key. Input help, object navigation and the review cursor are there.
* The add-on store. An add-on that keeps to NVDA's own interfaces works. One that calls Windows directly does not and cannot.
* Scripts that speak through Orca keep working: while Linda runs it answers `PresentMessage` on `org.gnome.Orca.Service`.
* `linda say`, which speaks one sentence from the command line and exits.

What does not work yet:

* Wayland sessions. Linda needs an X11 session.
* Braille displays.
* The login screen and screen lockers.
* The parts of NVDA built on Windows services: the magnifier, sound split, audio ducking, touch, Windows OCR, the installer, portable copies and the update check.
* Remote Access starts and can host or join a relay server, but nobody has yet controlled one machine from another with it.
* A Chromium or Electron program that was started before any screen reader was running stays silent until you restart it. Linda tells you which one a few seconds after it starts.
* Anything but 64-bit Intel and AMD machines, and any Unix but Linux.

## What you need

* A 64-bit Intel or AMD machine running Linux.
* An X11 session. MATE, Xfce and Cinnamon are X11 desktops. KDE Plasma has an X11 session on distributions that still ship it. GNOME has had none since GNOME 49.
* PipeWire as the sound server.
* The accessibility bus, which is the package `at-spi2-core` nearly everywhere and is usually installed already.
* The Nix package manager with flakes switched on. Nix is how Linda is built and delivered on every distribution for now. It installs beside your distribution's own packages and changes none of them.

## Try it

With the requirements in place, one command fetches, builds and starts Linda.

```
nix run github:Mudb0y/linda
```

There is no ready-made download yet, so the first start compiles parts of Linda on your machine.
That takes around twenty minutes on a recent computer and several gigabytes of disk, and it is silent while it works.
Every start after that is immediate.

Stop Orca, or any other screen reader, before you start Linda, or the two will talk over each other.
Starting Linda while another copy of Linda runs replaces that copy.

## Setting up, step by step

Linda is developed and used every day on NixOS with the MATE desktop.
On other distributions it runs through Nix in the same way, but those routes have had far less use.
The steps below follow each distribution's own documentation. If one of them is wrong for you, please open an issue.

### On every distribution

Check that you are in an X11 session. This prints `x11` when you are.

```
echo $XDG_SESSION_TYPE
```

If it prints `wayland`, sign out and pick another session at the login screen: one with X11 or Xorg in its name, or a desktop that only runs on X11, such as MATE or Xfce.

Check that PipeWire is running. This prints `active` when it is.

```
systemctl --user is-active pipewire
```

Switch flakes on for Nix, once Nix is installed. The first command makes the folder for Nix's settings and the second adds the line.

```
mkdir -p ~/.config/nix
```

```
echo "experimental-features = nix-command flakes" >> ~/.config/nix/nix.conf
```

### NixOS

Nothing needs installing to try it: `nix run github:Mudb0y/linda` is enough once flakes are on.

Your configuration needs the accessibility bus and PipeWire.

```nix
services.gnome.at-spi2-core.enable = true;
services.pipewire.enable = true;
services.pipewire.pulse.enable = true;
```

To have the `linda` command installed for good, add the flake as an input and its package to your system packages.

```nix
inputs.linda.url = "github:Mudb0y/linda";
```

```nix
environment.systemPackages = [ inputs.linda.packages.x86_64-linux.default ];
```

### Debian, Ubuntu and Linux Mint

Install what the Nix installer needs, and the accessibility bus.

```
sudo apt install curl xz-utils at-spi2-core
```

If PipeWire was not active above, this replaces PulseAudio with it. Sign out and in again afterwards.

```
sudo apt install pipewire-audio
```

Install Nix with its official installer, then open a new terminal.

```
curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install | sh -s -- --daemon
```

Debian 13 and later can use Debian's own package instead: `sudo apt install nix-setup-systemd`, then `sudo adduser $USER nix-users`, then sign out and in. The Nix in Debian 12 is too old.

Ubuntu's own desktop is GNOME, which has no X11 session from Ubuntu 25.10 on. Ubuntu MATE, Xubuntu and Linux Mint are X11 desktops.

### Fedora

Fedora uses PipeWire and has the accessibility bus installed.

From Fedora 44, Nix is in Fedora's own repositories. Install it and start its service.

```
sudo dnf install nix
```

```
sudo systemctl enable --now nix-daemon
```

On older Fedora releases, SELinux gets in the way of the official Nix installer's usual mode. The Nix manual describes the choices.

Fedora Workstation is GNOME, which has no X11 session from Fedora 43 on. The MATE, Xfce and Cinnamon spins are X11 desktops.

### Arch Linux and its relatives

Install Nix, the accessibility bus and PipeWire.

```
sudo pacman -S nix at-spi2-core pipewire pipewire-pulse wireplumber
```

Start the Nix service.

```
sudo systemctl enable --now nix-daemon
```

If `nix` then says it may not reach the daemon's socket, add yourself to the `nix-users` group with `sudo gpasswd -a $USER nix-users` and sign out and in.

### openSUSE Tumbleweed

Tumbleweed uses PipeWire and has Nix in its repositories. Install it and start its service.

```
sudo zypper install nix at-spi2-core
```

```
sudo systemctl enable --now nix-daemon
```

### Any other distribution

The official Nix installer works on any distribution that runs systemd.

```
curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install | sh -s -- --daemon
```

Then make sure of the three things under "On every distribution", and start Linda.

## Using it

Linda's commands are NVDA's. If you know NVDA, you know Linda.
Wherever NVDA's documentation says the NVDA key, Linda says the Linda key: Insert to begin with, and Caps Lock as well if you choose it in the welcome dialog or in the keyboard settings.

A few to start with:

* Linda+N opens Linda's menu, with the settings under Preferences.
* Linda+Q quits.
* Control stops speech.
* Linda+1 turns input help on and off. While it is on, every key says what it does.
* Linda+T reads the window's title, and Linda+Down arrow reads on from where you are.
* In a web page, H moves by heading, K by link, F by form field and T by table, Linda+F7 lists them, and Linda+Space switches between browse mode and focus mode.
* Linda+F2 passes the next key straight to the program.

Linda has no user guide of its own yet, but the guide in its Help menu, which is NVDA's, opens in your web browser, and F1 in a dialog opens it at the control you are on. [NVDA's user guide](https://download.nvaccess.org/documentation/userGuide.html) describes the same commands: read Linda where it says NVDA, and skip what is about Windows.

Your settings live in `~/.config/linda`, and the log of the current run is `~/.local/state/linda/nvda.log`.

### Programs that need telling

GTK programs and Firefox are readable as they are.
Chromium, Electron and Qt programs look, as they start, for whether a screen reader is there.
The simple way is to start Linda before them.
The lasting way is to set two variables for your whole session, for example in `~/.profile`, and sign in again.

```
export ACCESSIBILITY_ENABLED=1
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
```

## Installing it for good

This puts the `linda` and `linda-say` commands in your Nix profile, so that `linda` starts it from then on.

```
nix profile install github:Mudb0y/linda
```

This brings it up to date later.

```
nix profile upgrade --all
```

To have Linda start when you sign in, give your desktop an autostart entry called `linda.desktop`.
Save the following as `~/.config/autostart/linda.desktop`, with your own user name in place of YOU.
On NixOS, with the package in your system packages, the last line is just `Exec=linda`.

```
[Desktop Entry]
Type=Application
Name=Linda
Exec=/home/YOU/.nix-profile/bin/linda
```

## When something goes wrong

* Linda went quiet or stopped answering: start it again. The new copy replaces the old one within a few seconds.
* Linda will not start since you changed a setting or added an add-on: start it on a fresh settings folder with `linda -c ~/linda-fresh-settings`, or without add-ons with `linda --disable-addons`. With `nix run`, options go after two dashes, as in `nix run github:Mudb0y/linda -- --disable-addons`.
* There is no speech at all: check PipeWire as above, then try `linda say "Hello"`, which prints one line saying what it did or what failed. With `nix run` that is `nix run github:Mudb0y/linda#say -- "Hello"`.
* A program is not read: restart that program while Linda runs, or set the variables above.
* The screen reader's keys do nothing: check that the session is X11.

When you report a problem, say which distribution and desktop you use and attach `~/.local/state/linda/nvda.log`. The log can hold what you typed and what was on screen, so read through it first.

## Building and working on it

The flake is how Linda is built and tested today.

* `nix build` builds the package.
* `nix develop` opens a shell with everything the source tree needs, and `tools/build-artifacts` there builds what the tree runs with.
* The tests never touch your desktop. `tests/linux/rig` runs a command in a sandbox with its own display, session bus, accessibility bus and sound server, so `nix develop -c tests/linux/rig python -m pytest tests/linux` is safe to run with your own screen reader talking.

The package itself is built by a Meson build, which a distribution can use too.
`meson setup`, `meson compile` and `meson install` install the `linda` and `linda-say` commands, NVDA's Python tree under `share/linda/source`, the three native libraries under `lib/linda`, a desktop entry, the software centre's metadata, manual pages and the licence.
The build needs Python 3.13 with the modules Linda imports, cargo, a C++ compiler, PipeWire, AT-SPI, ICU and liblouis, and an option naming a copy of NV Access's nvda-cldr data.
Without other options it speaks with the distribution's own eSpeak NG, which has not been tested yet; the flake uses the eSpeak NG that NVDA pins.
`meson.options` lists every option.
There are no distribution packages yet.

Linda stays close to NVDA on purpose, so that NVDA's new releases can be merged in.
NVDA's files are changed as little as possible, each change is listed with its reason in `tools/upstream-edits.txt`, and `tools/check-upstream-edits` fails when the list and the tree disagree.
The Linux side lives in `source/linuxPlatform`, and `engine` holds the Rust parts.

## Licence and credit

Linda is free software under the GNU General Public License, version 2 or later, as NVDA is. [copying.txt](./copying.txt) has the full terms, including NVDA's own exceptions.

Nearly all of Linda is NVDA. The credit for it belongs to NV Access and to the hundreds of people in [contributors.txt](./contributors.txt).
NVDA is a registered trademark of NV Access Limited.
