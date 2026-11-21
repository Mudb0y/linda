# logind decides once, as it starts, whether seat0 has consoles, by whether /dev/tty0 exists
# (manager_connect_console in systemd's logind.c). The node is taken away for that moment alone and
# put back before the display manager starts, so that the kernel, the X server and the helper have
# their consoles as ever while logind places every session of seat0 on none: VTNr 0, which it
# allows on seat0 only when it finds no consoles there.
{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  systemd.services.ground-hide-consoles = {
    wantedBy = [ "systemd-logind.service" ];
    before = [ "systemd-logind.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${pkgs.coreutils}/bin/rm -f /dev/tty0";
    };
  };
  systemd.services.ground-show-consoles = {
    wantedBy = [ "display-manager.service" ];
    after = [ "systemd-logind.service" ];
    before = [ "display-manager.service" ];
    serviceConfig = {
      Type = "oneshot";
      ExecStart = [
        "${pkgs.coreutils}/bin/mknod -m 0620 /dev/tty0 c 4 0"
        "${pkgs.coreutils}/bin/chgrp tty /dev/tty0"
      ];
    };
  };
}
