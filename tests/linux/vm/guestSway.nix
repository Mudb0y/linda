# The sway guest the cases named wlSway boot: the cheapest real Wayland desktop, and the one whose
# focus a test can move and time through the compositor's own IPC. lightdm signs a user in without a
# password into a sway session on seat0, which logind counts as a Wayland session as it would a
# user's, with the Polish layout for every keyboard sway has, the helper's virtual keyboard among
# them, the session bus and the accessibility bus. The session runner is started by sway's own exec,
# so it and what it starts belong to the logind session. Linda is not part of it, as in guest.nix.
#
# Whether sway is built with Xwayland is programs.sway.xwayland.enable, which a case may set to false
# in its cases/NAME.nix. Whether a session uses it is the variant file's line, "xwayland enable"
# (Xwayland started when an X client first connects, and DISPLAY given to every program) or
# "xwayland disable", which sway reads only as it starts; a case may write the file and start the
# session again to run the other variant in the same boot.
{ tools }:
{
  config,
  pkgs,
  lib,
  ...
}:

let
  uid = 1000;
  # sway takes SWAYSOCK from its environment only if nothing is at that path yet, and otherwise
  # makes a name of its own (sway/ipc-server.c, ipc_init).
  swaysock = "/tmp/sway-ipc.sock";
  variant = "/run/ground-sway/xwayland.conf";
  xwayland = config.programs.sway.xwayland.enable;
  swaymsg = "${config.programs.sway.package}/bin/swaymsg";
  # Run by sway's exec; the session's environment is sway's, with a PATH of its own.
  runner = pkgs.writeShellScript "ground-session-sway" ''
    export PATH=/run/wrappers/bin:/run/current-system/sw/bin:$PATH
    exec ${tools}/bin/ground-session serve >/tmp/ground/session.log 2>&1
  '';
  # focusSway.py run as a program of its own, for its window watcher.
  swayTools = pkgs.writeShellScriptBin "ground-sway" ''
    exec ${pkgs.python3.interpreter} ${tools}/libexec/focusSway.py "$@"
  '';
  # What ground.py reads to know how to wait for this guest, as its Ground.boot describes; swaysock,
  # xwayland and variant are focusSway.py's.
  guest = {
    kind = "sway";
    # libwayland's first free name; sway takes no name of its own choosing.
    socket = "/run/user/${toString uid}/wayland-1";
    # sway answers on its IPC socket once its configuration is read and its exec lines started.
    started = "${swaymsg} -s ${swaysock} -t get_version >/dev/null";
    ready = [
      "/run/current-system/sw/bin/ground-focus"
      "ready"
    ];
    devices = [
      "/run/current-system/sw/bin/ground-focus"
      "devices"
      (toString uid)
    ];
    inherit swaysock xwayland variant;
  };
in
{
  virtualisation = {
    diskImage = null;
    memorySize = 2048;
    cores = 2;
    # wlroots finds no DRM device on QEMU's standard VGA (nixos/tests/sway.nix).
    qemu.options = [ "-vga none -device virtio-gpu-pci" ];
  };

  boot.kernelModules = [ "uinput" ];

  users.users.alice = {
    isNormalUser = true;
    inherit uid;
  };

  # LightDM requires the X server's options, but starts no X server for a Wayland session.
  services.xserver = {
    enable = true;
    displayManager.lightdm.enable = true;
  };
  services.displayManager = {
    defaultSession = "sway";
    autoLogin = {
      enable = true;
      user = "alice";
    };
  };

  programs.sway = {
    enable = true;
    extraOptions = [
      "--config"
      "/etc/ground-sway.conf"
    ];
    # Nothing the configuration below runs.
    extraPackages = [ ];
  };

  # Through PAM, so that they reach the session lightdm starts.
  environment.sessionVariables = {
    SWAYSOCK = swaysock;
    # The GLES2 renderer does not start in the guest (nixos/tests/sway.nix).
    WLR_RENDERER = "pixman";
  };

  environment.etc."ground-sway.conf".text = ''
    # What NixOS gives every sway session: the session's variables put in the user manager's
    # environment, and sway-session.target.
    include /etc/sway/config.d/*
    include ${variant}
    # The owner's layout for every keyboard, the uhid one and the helper's virtual one among them.
    input type:keyboard xkb_layout pl
    exec ${runner}
  '';
  environment.etc."ground/guest.json".text = builtins.toJSON guest;

  environment.systemPackages = [
    tools
    swayTools
  ];

  services.gnome.at-spi2-core.enable = true;

  systemd.tmpfiles.rules = [
    "d /tmp/ground 1777 root root -"
    "d /run/ground-sway 0755 root root -"
    "f ${variant} 0644 root root - xwayland ${if xwayland then "enable" else "disable"}"
  ];

  systemd.services.ground-record-evdev = {
    wantedBy = [ "multi-user.target" ];
    after = [ "systemd-tmpfiles-setup.service" ];
    serviceConfig.ExecStart = "${tools}/bin/ground-record evdev /tmp/ground/evdev.jsonl";
  };

  documentation.enable = false;
}
