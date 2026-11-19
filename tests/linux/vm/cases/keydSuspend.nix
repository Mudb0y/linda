# systemd's own watchdog is timed on the monotonic clock with nothing for a suspend, and kills a
# helper frozen for longer than WatchdogSec; it is set long here, so that the case shows what the
# helper itself does across a freeze.
{ lib, ... }:
{
  imports = [ ../keyd.nix ];
  systemd.services.linda-keyd.serviceConfig.WatchdogSec = lib.mkForce "60s";
}
