# The Plasma guest the cases named wlPlasma boot: what a newcomer's KDE Plasma is as far as Linda is
# concerned, and no further. Plasma Login Manager signs a user in without a password into a Plasma
# Wayland session on seat0, KWin with plasmashell, with the Polish layout as the only one KWin knows,
# the session bus and the accessibility bus, and no Orca. The session runner comes from an XDG
# autostart file, as Linda would, so it runs under the user's systemd manager and outside the logind
# session. Linda is not part of it, as in guest.nix.
#
# After the pinned nixpkgs' own plasma6 test, less the applications Plasma installs beside the shell.
{ tools }:
{ pkgs, ... }:

let
  uid = 1000;
  # Run by the autostart entry; the user manager's environment is the session's, with a PATH of its
  # own.
  runner = pkgs.writeShellScript "ground-session-autostart" ''
    export PATH=/run/wrappers/bin:/run/current-system/sw/bin:$PATH
    exec ${tools}/bin/ground-session serve >/tmp/ground/session.log 2>&1
  '';
  # The native Qt 6 window, which guest/focusPlasma.py holds because this guest alone needs it. It
  # takes the platform plugins from the session's own QT_PLUGIN_PATH, which is the Qt the shell runs
  # on, and registers on the accessibility bus whether or not a reader has claimed it, as a GTK
  # program does, so that AT-SPI can say when it is active.
  python = pkgs.python3.withPackages (ps: [
    ps.pyqt6
    ps.evdev
  ]);
  qtWindow = pkgs.writeShellScriptBin "ground-window-qt" ''
    export GROUND_XKBCOMMON=${pkgs.libxkbcommon}/lib/libxkbcommon.so.0
    export QT_QPA_PLATFORM=wayland
    export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
    exec ${python.interpreter} ${tools}/libexec/focusPlasma.py window "$@"
  '';
  # A process whose program, by the first word of its command line, is name or name's wrapped binary.
  programPattern = name: "^[^ ]*${name}(-wrapped)?( |$)";
  # What ground.py reads to know how to wait for this guest, as its Ground.boot describes.
  guest = {
    kind = "plasma";
    # KWin's wrapper numbers its sockets from 1 (src/helpers/wayland_wrapper/wl-socket.c in 6.8.3).
    socket = "/run/user/${toString uid}/wayland-1";
    # The splash screen goes once Plasma's start-up has reached the desktop. Both programs are
    # wrapped, so their process names are the wrappers'.
    started = "pgrep -u ${toString uid} -f '${programPattern "plasmashell"}' >/dev/null && ! pgrep -u ${toString uid} -f '${programPattern "ksplashqml"}' >/dev/null";
    ready = [
      "/run/current-system/sw/bin/ground-focus"
      "ready"
    ];
    devices = [
      "/run/current-system/sw/bin/ground-focus"
      "devices"
      (toString uid)
    ];
  };
in
{
  virtualisation = {
    diskImage = null;
    memorySize = 4096;
    cores = 2;
  };

  boot.kernelModules = [ "uinput" ];

  users.users.alice = {
    isNormalUser = true;
    inherit uid;
  };

  services.xserver.enable = true;
  services.displayManager = {
    plasma-login-manager.enable = true;
    autoLogin = {
      enable = true;
      user = "alice";
    };
  };
  services.desktopManager.plasma6.enable = true;
  services.orca.enable = false;
  # On NixOS only Orca's module turns the accessibility bus on for Plasma, so without Orca a reader
  # has to, as the GNOME and MATE modules do for themselves.
  services.gnome.at-spi2-core.enable = true;
  environment.plasma6.excludePackages = with pkgs.kdePackages; [
    ark
    baloo-widgets
    dolphin
    dolphin-plugins
    elisa
    ffmpegthumbs
    gwenview
    kate
    khelpcenter
    konsole
    krdp
    ktexteditor
    okular
    plasma-browser-integration
    plasma-keyboard
    plasma-workspace-wallpapers
    qtvirtualkeyboard
    spectacle
    union
  ];
  # Akonadi with its database, and Discover, which fwupd brings in: neither is the shell.
  programs.kde-pim.enable = false;
  services.fwupd.enable = false;

  # The owner's layout, as KWin takes it: kxkbrc, read through the XDG configuration path. Nothing
  # is put in alice's own configuration, so that a pass shows KWin honours the system-wide file.
  environment.etc."xdg/kxkbrc".text = ''
    [Layout]
    LayoutList=pl
    Use=true
  '';
  # The screen locker locks only when a case asks logind to; left to itself it would lock after five
  # idle minutes in the middle of a case.
  environment.etc."xdg/kscreenlockerrc".text = ''
    [Daemon]
    Autolock=false
    LockOnResume=false
  '';

  environment.etc."xdg/autostart/ground-session.desktop".text = ''
    [Desktop Entry]
    Type=Application
    Name=Ground session runner
    Exec=${runner}
    NoDisplay=true
  '';
  environment.etc."ground/guest.json".text = builtins.toJSON guest;

  environment.systemPackages = [
    tools
    qtWindow
    pkgs.kdotool
  ];

  systemd.tmpfiles.rules = [ "d /tmp/ground 1777 root root -" ];

  systemd.services.ground-record-evdev = {
    wantedBy = [ "multi-user.target" ];
    after = [ "systemd-tmpfiles-setup.service" ];
    serviceConfig.ExecStart = "${tools}/bin/ground-record evdev /tmp/ground/evdev.jsonl";
  };

  documentation.enable = false;
}
