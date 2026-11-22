# The Plasma guest of the older desktops, which the cases named wl65Plasma and wl66Plasma boot from
# the stable release that ships that Plasma (default.nix): guestPlasma.nix's guest, as a user of that
# release would have it. A display manager signs a user in without a password into a Plasma Wayland
# session on seat0, KWin with plasmashell, with the Polish layout as the only one KWin knows, the
# session bus and the accessibility bus, and no Orca; the session runner comes from an XDG autostart
# file. Linda is not part of it, as in guest.nix.
#
# 25.11 has no Plasma Login Manager, so there SDDM signs in, with its greeter on Wayland and the
# Wayland session chosen, which that release's own plasma6 test leaves to X11. Where the release has
# the Plasma Login Manager, it signs in, as in guestPlasma.nix and that release's own test.
{ tools }:
{
  pkgs,
  lib,
  options,
  ...
}:

let
  uid = 1000;
  hasLoginManager = options.services.displayManager ? plasma-login-manager;
  runner = pkgs.writeShellScript "ground-session-autostart" ''
    export PATH=/run/wrappers/bin:/run/current-system/sw/bin:$PATH
    exec ${tools}/bin/ground-session serve >/tmp/ground/session.log 2>&1
  '';
  # The native Qt 6 window, as guestPlasma.nix has it, on this release's Qt, which is the shell's.
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
  programPattern = name: "^[^ ]*${name}(-wrapped)?( |$)";
  guest = {
    kind = "plasma";
    # KWin's wrapper numbers its sockets from 0 in 6.5.6 and 6.6.6, and from 1 by 6.8.3.
    socket = "/run/user/${toString uid}/wayland-0";
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
  # guestPlasma.nix's list, less what an older release does not have yet.
  excluded = lib.filter (name: pkgs.kdePackages ? ${name}) [
    "ark"
    "baloo-widgets"
    "dolphin"
    "dolphin-plugins"
    "elisa"
    "ffmpegthumbs"
    "gwenview"
    "kate"
    "khelpcenter"
    "konsole"
    "krdp"
    "ktexteditor"
    "okular"
    "plasma-browser-integration"
    "plasma-keyboard"
    "plasma-workspace-wallpapers"
    "qtvirtualkeyboard"
    "spectacle"
    "union"
  ];
in
{
  config = lib.mkMerge [
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
      services.displayManager.autoLogin = {
        enable = true;
        user = "alice";
      };
      services.desktopManager.plasma6.enable = true;
      services.orca.enable = false;
      services.gnome.at-spi2-core.enable = true;
      environment.plasma6.excludePackages = map (name: pkgs.kdePackages.${name}) excluded;
      programs.kde-pim.enable = false;
      services.fwupd.enable = false;

      environment.etc."xdg/kxkbrc".text = ''
        [Layout]
        LayoutList=pl
        Use=true
      '';
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
    (
      if hasLoginManager then
        { services.displayManager.plasma-login-manager.enable = true; }
      else
        {
          services.displayManager = {
            sddm = {
              enable = true;
              wayland.enable = true;
            };
            defaultSession = "plasma";
          };
        }
    )
  ];
}
