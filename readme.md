# Linda

Linda Is Non-visual Desktop Access: a screen reader for the Linux desktop.

Linda is NVDA, the screen reader most blind people use on Windows, brought to Linux.
The commands, the settings, browse mode for web pages and the add-on interface are NVDA's own, so what you know from NVDA works here the way you expect.
Underneath, Linda reads programs through AT-SPI and speaks through PipeWire.
It takes its keys from the X server in an X11 session, and in a Wayland session through linda-keyd, a small system service of its own that holds the keyboards in the kernel.

Linda is based on NVDA 2026.3.1 by NV Access and the NVDA contributors.
It is an independent project and is not affiliated with or endorsed by NV Access.
Please report problems with Linda [here](https://github.com/Mudb0y/linda/issues), never to NV Access.

## Where it stands

Linda is young and changes every day.
Its author uses it as their only screen reader, in an X11 session with the MATE desktop, so the daily paths there work, and plenty of corners are still rough.
Wayland sessions are newer: they have been tested in virtual machines, with a first report from a real machine, and the section "Wayland sessions" below says what was tested on which desktop.

What works today in an X11 session:

* Speech with eSpeak NG and with [openevv](https://github.com/Mudb0y/openevv). Both come with Linda.
* GTK and Qt programs, the MATE desktop, terminals.
* Firefox, Chromium and Electron programs such as Discord, in browse mode: single letter navigation, the elements list, find, tables, forms and focus mode, live regions, and in Firefox a change of voice when the page changes language.
* NVDA's keyboard commands in the desktop and laptop layouts, with Insert, Caps Lock or both as the modifier key, which Linda calls the Linda key. Input help, object navigation and the review cursor are there.
* The add-on store. An add-on that keeps to NVDA's own interfaces works. One that calls Windows directly does not and cannot.
* Scripts that speak through Orca keep working: while Linda runs it answers `PresentMessage` on `org.gnome.Orca.Service`.
* `linda say`, which speaks one sentence from the command line and exits.
* Braille displays, through BRLTTY.
* Remote Access: controlling one computer from another through a relay server, with NVDA on Windows at the other end too.
* MATE's screen locker: Linda says that the screen is locked, reads only the unlock prompt, and speaks none of the password.

What does not work yet:

* The login screen.
* The parts of NVDA built on Windows services: the magnifier, sound split, audio ducking, touch, Windows OCR, the installer, portable copies and the update check.
* A Chromium or Electron program that was started before any screen reader was running stays silent until you restart it. Linda tells you which one a few seconds after it starts.
* Steam's gamescope session, which has not been tested.
* Screen lockers other than MATE's, such as those of Xfce and Cinnamon, light-locker and xscreensaver, which have not been tested.
* Anything but 64-bit Intel and AMD machines, and any Unix but Linux.

## What you need

* A 64-bit Intel or AMD machine running Linux.
* An X11 session, or one of the Wayland sessions under "Wayland sessions" with linda-keyd installed. MATE, Xfce and Cinnamon are X11 desktops. KDE Plasma has an X11 session on distributions that still ship it. GNOME has had none since GNOME 49.
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

In a Wayland session, install linda-keyd first, with the screen reader you use today. Without it Linda starts, speaks and reads the screen, but has no keys.

## Setting up, step by step

Linda is developed and used every day on NixOS with the MATE desktop.
On other distributions it runs through Nix in the same way, but those routes have had far less use.
The steps below follow each distribution's own documentation. If one of them is wrong for you, please open an issue.

### On every distribution

Check which kind of session you are in. This prints `x11` or `wayland`.

```
echo $XDG_SESSION_TYPE
```

If it prints `wayland`, Linda needs linda-keyd, which the NixOS section below and the section "linda-keyd on other distributions" install, and "Wayland sessions" says which desktops have been tested.
If you would rather stay with X11, sign out and pick another session at the login screen: one with X11 or Xorg in its name, or a desktop that only runs on X11, such as MATE or Xfce.

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

For braille, BRLTTY has to be running and driving your display, as your distribution's `brltty` package usually does once the display is plugged in, and you have to be in the `brlapi` group, which may use BRLTTY's BrlAPI. This adds you to the group; sign out and in afterwards.

```
sudo usermod -aG brlapi $USER
```

On NixOS, `services.brltty.enable = true;` runs BRLTTY and makes the group instead, and adding `"brlapi"` to your user's `extraGroups` puts you in it. Linda's default braille display, Automatic, then finds BRLTTY by itself.

### NixOS

Nothing needs installing to try it in an X11 session: `nix run github:Mudb0y/linda` is enough once flakes are on.

Your configuration needs the accessibility bus and PipeWire.

```nix
services.gnome.at-spi2-core.enable = true;
services.pipewire.enable = true;
services.pipewire.pulse.enable = true;
```

Keep the first of those lines on KDE Plasma too. NixOS's Plasma module turns the accessibility bus on only through Orca's option, `services.orca.enable`, which it sets by default, so with Orca turned off and without that line Linda has nothing to read.

To have the `linda` command installed for good, add the flake as an input and its package to your system packages.

```nix
inputs.linda.url = "github:Mudb0y/linda";
```

```nix
environment.systemPackages = [ inputs.linda.packages.x86_64-linux.default ];
```

For a Wayland session, add linda-keyd from the same flake: import its module and turn the service on. In an X11 session it is optional, and Linda takes its keys through it while it runs, unless "Take keys from" in the Advanced settings is set to the X display alone.

```nix
imports = [ inputs.linda.nixosModules.linda-keyd ];
```

```nix
services.linda-keyd.enable = true;
```

After the rebuild, this prints `active` when the helper runs.

```
systemctl is-active linda-keyd
```

linda-keyd lets in a program of any local user whose session is in front. To let in only the members of one group, name the group, make it, and add `"linda"` to your user's `extraGroups`. A change of membership counts from Linda's next start, with no new sign-in.

```nix
services.linda-keyd.allowedGroup = "linda";
```

```nix
users.groups.linda = { };
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

Ubuntu's own desktop is GNOME, which has no X11 session from Ubuntu 25.10 on, so there Linda needs linda-keyd. Ubuntu MATE, Xubuntu and Linux Mint are X11 desktops.

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

Fedora Workstation is GNOME, which has no X11 session from Fedora 43 on, so there Linda needs linda-keyd. The MATE, Xfce and Cinnamon spins are X11 desktops.

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

### linda-keyd on other distributions

Nix cannot install a system service outside NixOS, so there linda-keyd comes as a package of your distribution's own kind, for Debian and Ubuntu, Fedora, and Arch Linux, on 64-bit Intel and AMD processors.
The helper in it is linked statically, so it needs none of your distribution's libraries, and the service it installs is the one the NixOS module runs.
Each package is installed and removed in a container of its distribution whenever it is built, but none has yet been tried on a real machine, so please say how it went.

On Debian and Ubuntu, this downloads the package into the folder you are in, since apt cannot install from a web address.

```
curl -fLO https://github.com/Mudb0y/linda/releases/download/linda-keyd/linda-keyd_amd64.deb
```

This installs it, makes the helper's system user, loads the kernel's virtual keyboard module, and starts the service now and at every boot, as Debian does with a service a package installs.

```
sudo apt install ./linda-keyd_amd64.deb
```

On Fedora, this downloads and installs the package, makes the helper's system user and loads the kernel's virtual keyboard module. The packages are not signed until there is a package repository for them, and Fedora 45 refuses a package without a signature unless `--no-gpgchecks` is given.

```
sudo dnf install --no-gpgchecks https://github.com/Mudb0y/linda/releases/download/linda-keyd/linda-keyd.x86_64.rpm
```

On Arch Linux, this downloads the package into the folder you are in, since pacman takes a package without a signature only from a file.

```
curl -fLO https://github.com/Mudb0y/linda/releases/download/linda-keyd/linda-keyd-x86_64.pkg.tar.zst
```

This installs it, makes the helper's system user and loads the kernel's virtual keyboard module.

```
sudo pacman -U ./linda-keyd-x86_64.pkg.tar.zst
```

Fedora and Arch do not start a service a package installs. On either, this starts it now and at every boot.

```
sudo systemctl enable --now linda-keyd
```

On any of them, this prints `active` when the helper runs.

```
systemctl is-active linda-keyd
```

Updates are not automatic: until there is a package repository, your distribution's updater does not know about linda-keyd.
When you update Linda, update the helper too, by running the same download and install commands again, which replace it with the newest build.
Debian, Ubuntu and Fedora restart the service after the update; on Arch restart it yourself with `sudo systemctl restart linda-keyd`.
Removing the package stops the service: `sudo apt remove linda-keyd`, `sudo dnf remove linda-keyd` or `sudo pacman -R linda-keyd`.

On any other distribution or processor, openSUSE among them until its package has been tried, build linda-keyd from Linda's source with Meson and install it as your distribution's own packages would be.
This has been built and installed by Linda's tests, but not yet on another distribution.

The build needs git, Meson 1.4 or later, Ninja, pkg-config, a C++ compiler with C++20, Rust 1.89 or later with cargo, libclang, and PipeWire's development files, which are in your distribution's packages under those names or close to them. libclang is `libclang-dev` on Debian and Ubuntu, `clang-devel` on Fedora and `clang` on Arch.
Where the distribution's own Rust is older than 1.89, as Debian 13's is, [rustup](https://rustup.rs) installs a newer one for your user.

Fetch the source and go into it.

```
git clone https://github.com/Mudb0y/linda
```

```
cd linda
```

Set up a build of the libraries and the helper alone, with the files that run it as a system service. The prefix is `/usr` because polkit reads the helper's action only from there.

```
meson setup build-keyd --prefix=/usr -Dprogram=false -Dkeyd_service=true
```

Build it as yourself, which fetches the Rust crates it needs the first time, and then install it without building again, so that nothing is built as root and a Rust from rustup, which root does not have, is not needed there.

```
meson compile -C build-keyd
```

```
sudo meson install -C build-keyd --no-rebuild
```

Make the helper's system user, load the kernel's virtual keyboard module, which happens by itself from the next boot on, and start the service now and at every boot.

```
sudo systemd-sysusers
```

```
sudo modprobe uinput
```

```
sudo systemctl daemon-reload
```

```
sudo systemctl enable --now linda-keyd
```

This prints `active` when it runs.

```
systemctl is-active linda-keyd
```

When you update Linda, update the helper too, so that Linda can use everything the helper offers it: `git pull` in the source folder, then `meson compile -C build-keyd` and `sudo meson install -C build-keyd --no-rebuild` again, then `sudo systemctl restart linda-keyd`.

To let in only the members of one group, as the NixOS option `allowedGroup` does, save this as `/etc/polkit-1/rules.d/50-linda-keyd.rules`, with the group's name in place of linda, and add yourself to that group.

```
polkit.addRule(function(action, subject) {
  if (action.id == "io.github.mudb0y.linda.keyd.capture" && !subject.isInGroup("linda")) {
    return polkit.Result.NO;
  }
});
```

## Wayland sessions

A Wayland desktop gives the keys to the program you are typing into and to no one else, so Linda takes them through linda-keyd.
The helper holds the keyboards in the kernel while Linda runs in the session in front, gives Linda each key first, and passes on through a virtual keyboard every key Linda does not keep.
Without it Linda starts, speaks and reads the screen, and says once: "Linda cannot take keys in this Wayland session until linda-keyd runs."
If it had no keys at first, as when linda-keyd starts after Linda, it says "Linda has the keyboard" once it takes them.
As it takes the keyboard, linda-keyd checks whether the desktop reads the keyboard it passes keys on through. GNOME and sway showed that at once in the tests, and there a start with linda-keyd running says nothing about the keyboard at all.
Where the desktop gives no such sign, Linda says at each start "If typing stops reaching programs, hold Backspace, Escape and Enter together.", until it has seen what you type reach a program on that desktop.

Everything below was tested in virtual machines running NixOS, by tests that type on a virtual keyboard and read what Linda said.
The first report from a real machine, GNOME on Arch Linux, says it works there. More reports of how it goes are very welcome.

What was tested, by desktop:

* GNOME 51: Linda starts and speaks; its commands with Insert and with Caps Lock as the Linda key; typing echo, fast and at a person's pace; the Polish layout with its AltGr letters; browse mode in Firefox, tested with H for the next heading; Polish and US English as two input sources, switched; Remote Access, with the controlling computer typing through Linda; Linda's menu and its settings dialog, each ready within about a second; the lock screen, with its prompt read and nothing of the password spoken; GNOME's Alt+Tab switcher, its run dialog, and its password prompts for administrator rights and for the keyring; copying and reading the clipboard; a switch to a text console and back.
* GNOME 49 and 50: Linda starts and speaks; its commands with Insert and with Caps Lock; letters echoed; the Polish layout with its AltGr letters; a switch to a text console and back. These were tested with GTK's own Wayland input method, not with IBus, which NixOS turns on by default on those releases.
* KDE Plasma 6.8: Linda starts and speaks; its commands with Insert and with Caps Lock; letters echoed; the Polish layout with its AltGr letters, and two layouts switched with Meta+Alt+K; browse mode in Firefox, tested with H; Linda's menu and its settings dialog, each ready within a second; the lock screen, with its prompt read and nothing of the password spoken; KWin's Alt+Tab, KRunner, and the title and password field of Plasma's prompt for administrator rights; copying and reading the clipboard; a switch to a text console and back.
* KDE Plasma 6.5 and 6.6: Linda starts and speaks; its commands with Insert and with Caps Lock; letters echoed; the Polish layout with its AltGr letters, and two layouts switched with Meta+Alt+K; a switch to a text console and back.
* sway 1.12: Linda starts and speaks; its commands with Insert and with Caps Lock; letters echoed; the Polish layout with its AltGr letters, and two layouts switched; Linda's menu and its settings dialog, each ready within a second; copying and reading the clipboard.

On every one of them, Linda took the keyboard back by itself after linda-keyd was killed and restarted; when Linda was frozen, keys reached programs from the keyboard itself after about three seconds; and the emergency chord let go of the keyboard, Linda said how to get it back, and that way back worked.
What was typed was in neither Linda's log nor the helper's journal, beyond what Linda's log holds of typing echo when debug logging is on, as it does in an X11 session too.
In the test rig's own headless Mutter and sway, browse mode's single letters also work in Chromium and in an Electron program; no full desktop has tested those yet.
Other Wayland desktops, such as COSMIC, Hyprland, niri, river and labwc, have not been tested at all. Linda tries the same way there, and takes the keyboard layout from the system's settings, as `localectl` sets them, and not from the desktop's own.

What does not work yet in a Wayland session:

* The mouse commands and mouse tracking, the Screen Curtain and Visual Highlight. Each of their commands says "Not available in this Wayland session". A Screen Curtain switched on in the settings is not turned on, and Linda says once that the screen can be seen.
* On GNOME, a dialog of Linda's that is brought forward from behind another window takes one to two seconds to come up, and may first be announced as not having the keyboard, with the advice to press Alt+Tab, before GNOME gives it the keyboard.
* On GNOME, a letter typed within about a tenth of a second after Alt+Tab goes wrong. Switching to Firefox in browse mode, it reaches the page as typing instead of moving by Linda's single letter navigation; switching away from Firefox, it reaches the other window, and Linda moves in the page it left as well.
* On GNOME, notifications are not spoken.
* On GNOME, the number of stars spoken while a password is typed does not match the letters typed. None of the password is spoken.
* On Plasma, the message and the failure notice of the prompt for administrator rights are not read.
* On Plasma 6.5 and 6.6, KRunner takes a second or more to open the first time after sign-in, and letters typed before it is there are lost.
* On sway, after a switch to a text console and back, the first key you type is spent giving the window the keyboard back, and Linda's focus stays on the desktop until then; and keys typed in the first second or so after coming back can be lost.
* On sway, the keyboards' lock lights go out when linda-keyd takes them, Linda can be told that Num Lock is off while it is on, and Caps Lock pressed while linda-keyd holds the keyboard turns Num Lock off.
* On sway, keyboard options set in sway's own configuration, such as `xkb_options`, are not seen by Linda.
* A keyboard that is also a pointing device, as some wireless keyboards with a touchpad are, is left alone by linda-keyd: its keys reach programs without Linda, which says so once.
* With Orca running beside Linda, both speak everything you type, and Orca's commands made with Insert are out of reach. On GNOME Linda says so once, and how to turn Orca off.
* Lock screens other than those of GNOME 51 and Plasma 6.8 have not been tested: neither those of GNOME 49 and 50 and Plasma 6.5 and 6.6, nor swaylock or any other locker on sway.
* Steam's gamescope session has not been tested, and nothing is claimed for it yet.

### The emergency chord

If typing stops reaching programs, hold Backspace, Escape and Enter together. linda-keyd lets go of every keyboard at once, and the keys go straight to programs again.
In a Wayland session Linda then has no keys until you give them back, and it says how: on GNOME and Plasma, press Alt+F2, type linda and press Enter, which starts Linda again, and needs Linda installed for good as below; elsewhere, run `linda --take-keyboard` in a terminal, or start Linda again.
In an X11 session Linda goes on reading keys from the X server, and Linda+Shift+F12 takes the keyboard back.

### Who linda-keyd lets in

linda-keyd hands the keys to one program at a time: a program of the user whose session is in front, as logind and polkit decide, and never one running in a Flatpak or Snap sandbox.
That can be any program of yours, not only Linda. If another program holds it, Linda says so: "Another program holds linda-keyd for this session, so Linda has no keys. If you started no other screen reader, something may be reading your keys."
The `allowedGroup` setting above narrows it to the members of one group.

### Testing a Wayland session

This is what to run and listen for when you try Linda on a Wayland desktop and report how it went.

In a terminal of the Wayland session, this prints what Linda knows of the session and exits. With `nix run` it is `nix run github:Mudb0y/linda -- --session-report`.

```
linda --session-report
```

Listen for the line beginning "Session kind", which should say wayland, the line beginning "Desktop", which names gnome, kde, sway or another, and the last line, which should say that linda-keyd's socket is there.

Then start Linda. On GNOME and sway a start says nothing about the keyboard. On other desktops you may hear, within a few seconds, "If typing stops reaching programs, hold Backspace, Escape and Enter together.", until Linda has seen your typing reach a program there. Either way, Linda+T should read the window's title.
If you hear instead that Linda cannot take keys until linda-keyd runs, that is the sign of trouble: check the helper with `systemctl is-active linda-keyd`.

Then try what you use every day, and the things in the lists above for your desktop: Linda+T for the window's title, typing into a text field, Linda+N for the menu, H in a web page in Firefox. Try the emergency chord once too, once you know the way back for your desktop.

When you report, give the desktop and its version, what `linda --session-report` printed, what you did and what you heard, and Linda's log as described below.

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

sway reads no autostart entries; there the line `exec linda` in sway's configuration does the same.

## When something goes wrong

* Linda went quiet or stopped answering: start it again. The new copy replaces the old one within a few seconds.
* Linda will not start since you changed a setting or added an add-on: start it on a fresh settings folder with `linda -c ~/linda-fresh-settings`, or without add-ons with `linda --disable-addons`. With `nix run`, options go after two dashes, as in `nix run github:Mudb0y/linda -- --disable-addons`.
* There is no speech at all: check PipeWire as above, then try `linda say "Hello"`, which prints one line saying what it did or what failed. With `nix run` that is `nix run github:Mudb0y/linda#say -- "Hello"`.
* A program is not read: restart that program while Linda runs, or set the variables above.
* The screen reader's keys do nothing: run `linda --session-report`, which says what kind of session Linda finds and whether linda-keyd is there. In a Wayland session, check that linda-keyd runs with `systemctl is-active linda-keyd`.
* Typing stopped reaching programs: hold Backspace, Escape and Enter together, as under "The emergency chord".

When you report a problem, say which distribution and desktop you use and attach `~/.local/state/linda/nvda.log`.

**Read a log before you share it.** At the default logging level it holds neither what you typed nor what Linda said. With the level set to "input/output" or "debug", in the General settings or with `--debug-logging`, it records everything Linda speaks, as NVDA does on Windows: each character you type, because typing echo is speech, and everything Linda reads to you. Passwords typed into password fields are spoken as "star" and are not in it.

## Building and working on it

The flake is how Linda is built and tested today.

* `nix build` builds the package.
* `nix develop` opens a shell with everything the source tree needs, and `tools/build-artifacts` there builds what the tree runs with.
* The tests never touch your desktop. `tests/linux/rig` runs a command in a sandbox with its own display, session bus, accessibility bus and sound server, so `nix develop -c tests/linux/rig python -m pytest tests/linux` is safe to run with your own screen reader talking. linda-keyd's tests that take real keyboards run only in virtual machines, as the flake's checks.

The package itself is built by a Meson build, which a distribution can use too.
`meson setup`, `meson compile` and `meson install` install the `linda` and `linda-say` commands, NVDA's Python tree under `share/linda/source`, the three native libraries and linda-keyd under `lib/linda`, a desktop entry, the software centre's metadata, manual pages and the licence.
With the option `keyd_service` it also installs what runs linda-keyd as a system service: its systemd unit, its system user, the loading of the uinput module and its polkit action.
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
