# The GNOME guest the cases named wlGnome boot: what a newcomer's GNOME is as far as Linda is
# concerned, and no further. gdm signs a user in without a password into a GNOME Shell Wayland
# session on seat0, with the shell in safe mode as every real one runs, the Polish layout as the
# desktop's only input source, the session bus and the accessibility bus, and no Orca. The session
# runner comes from an XDG autostart file, as Linda would, so it runs under the user's systemd
# manager and outside the logind session. Linda is not part of it, as in guest.nix.
{ tools }:
{ pkgs, lib, ... }:

let
  uid = 1000;
  # Run by the autostart entry; the user manager's environment is the session's, with a PATH of its
  # own.
  runner = pkgs.writeShellScript "ground-session-autostart" ''
    export PATH=/run/wrappers/bin:/run/current-system/sw/bin:$PATH
    exec ${tools}/bin/ground-session serve >/tmp/ground/session.log 2>&1
  '';
  # What ground.py reads to know how to wait for this guest, as its Ground.boot describes.
  guest = {
    kind = "gnome";
    socket = "/run/user/${toString uid}/wayland-0";
    # The shell logs this message ID once its start-up animation is over (js/ui/main.js).
    started = "journalctl -b -q --no-pager _UID=${toString uid} MESSAGE_ID=f3ea493c22934e26811cd62abe8e203a | grep -q .";
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

  services.displayManager = {
    gdm.enable = true;
    autoLogin = {
      enable = true;
      user = "alice";
    };
  };
  services.desktopManager.gnome.enable = true;
  services.gnome = {
    core-apps.enable = false;
    gnome-initial-setup.enable = false;
    at-spi2-core.enable = true;
  };
  environment.gnome.excludePackages = [
    pkgs.orca
    pkgs.gnome-tour
  ];

  # The owner's layout, as GNOME takes it: the desktop's input sources, not the X server's layout.
  programs.dconf.profiles.user.databases = [
    {
      settings."org/gnome/desktop/input-sources".sources = [
        (lib.gvariant.mkTuple [
          "xkb"
          "pl"
        ])
      ];
    }
  ];

  environment.etc."xdg/autostart/ground-session.desktop".text = ''
    [Desktop Entry]
    Type=Application
    Name=Ground session runner
    Exec=${runner}
    NoDisplay=true
  '';
  environment.etc."ground/guest.json".text = builtins.toJSON guest;

  environment.systemPackages = [ tools ];

  systemd.tmpfiles.rules = [ "d /tmp/ground 1777 root root -" ];

  systemd.services.ground-record-evdev = {
    wantedBy = [ "multi-user.target" ];
    after = [ "systemd-tmpfiles-setup.service" ];
    serviceConfig.ExecStart = "${tools}/bin/ground-record evdev /tmp/ground/evdev.jsonl";
  };

  documentation.enable = false;
}
