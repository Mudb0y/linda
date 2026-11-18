# The guest every key capture test boots: the owner's desktop as far as keys are concerned, and no
# further. lightdm signs a user in without a password into an X session on seat0, whose X server
# reads the guest's kernel input devices through libinput, with marco as the window manager, the
# session bus and the accessibility bus. Linda is not part of it: each test hands the guest the
# package it tests, so that a change to Linda rebuilds no part of the guest.
{ tools }:
{ pkgs, ... }:

{
  virtualisation = {
    # The root file system in the guest's memory, so that no disk image is ever written.
    diskImage = null;
    memorySize = 2048;
    cores = 2;
  };

  # The QEMU monitor's keys reach the emulated keyboard; the fake keyboards are made through uinput.
  boot.kernelModules = [ "uinput" ];

  users.users.alice = {
    isNormalUser = true;
    uid = 1000;
  };

  services.xserver = {
    enable = true;
    # The owner's layout.
    xkb.layout = "pl";
    displayManager.lightdm.enable = true;
    # What MATE starts its window manager with.
    windowManager.session = [
      {
        name = "marco";
        start = ''
          ${pkgs.marco}/bin/marco --no-force-fullscreen &
          waitPID=$!
        '';
      }
    ];
    # Run by the session itself before marco, so that what the runner starts is the session's.
    displayManager.sessionCommands = ''
      ${tools}/bin/ground-session serve >/tmp/ground/session.log 2>&1 &
    '';
  };
  services.libinput.enable = true;
  services.displayManager = {
    defaultSession = "none+marco";
    autoLogin = {
      enable = true;
      user = "alice";
    };
  };
  services.gnome.at-spi2-core.enable = true;
  # marco keeps its settings in GSettings.
  programs.dconf.enable = true;

  environment.systemPackages = [
    tools
    pkgs.xdotool
    pkgs.xinput
    pkgs.xprop
  ];

  systemd.tmpfiles.rules = [ "d /tmp/ground 1777 root root -" ];

  # From boot, as root, so that it has every keyboard open before anything presses a key.
  systemd.services.ground-record-evdev = {
    wantedBy = [ "multi-user.target" ];
    after = [ "systemd-tmpfiles-setup.service" ];
    serviceConfig.ExecStart = "${tools}/bin/ground-record evdev /tmp/ground/evdev.jsonl";
  };

  documentation.enable = false;
}
