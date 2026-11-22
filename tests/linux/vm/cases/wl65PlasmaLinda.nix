# What wlPlasmaHelper and wlGnomeLinda each add to their guest, for the one boot that runs the
# helper's safety and then Linda's acceptance; nothing in wlGnomeLinda's module is GNOME's alone but
# the dconf module its tool is given, which a Plasma session does not read. gdbus tells KWin that
# kxkbrc changed, as Plasma's settings do.
{ pkgs, ... }:

{
  imports = [
    ./wlPlasmaHelper.nix
    ./wlGnomeLinda.nix
  ];
  environment.systemPackages = [ pkgs.glib ];
}
