# The helper, a document for browse mode below the lock, and no idle lock of GNOME's own in the middle
# of the case.
{ pkgs, lib, ... }:

{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [ pkgs.firefox ];
  programs.dconf.profiles.user.databases = [
    {
      settings."org/gnome/desktop/session".idle-delay = lib.gvariant.mkUint32 0;
    }
  ];
}
