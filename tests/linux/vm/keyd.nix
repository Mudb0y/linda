# What a test of linda-keyd adds to the guest, imported by its cases/NAME.nix: the helper turned on
# through the flake's own module, as the owner turns it on; uhid, through which the ground makes
# keyboards the helper takes; a second user, whose processes the helper must turn away; and the tools
# a test pokes the helper with.
{ pkgs, ... }:

{
  services.linda-keyd.enable = true;
  boot.kernelModules = [ "uhid" ];
  users.users.bob = {
    isNormalUser = true;
    uid = 1001;
  };
  # The clients' control sockets and records, made by whichever user each client runs as.
  systemd.tmpfiles.rules = [ "d /tmp/ground/keyd 1777 root root -" ];
  environment.systemPackages = [
    pkgs.acl
    pkgs.strace
    pkgs.kbd
    pkgs.xset
  ];
}
