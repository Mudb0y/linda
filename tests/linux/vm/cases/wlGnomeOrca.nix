# The GNOME guest that keeps Orca, for experiment E11: what wlGnomeLinda's guest has, and Orca as the
# GNOME module installs it when it is not excluded, with speech-dispatcher, which it speaks through.
# The case starts Orca itself, with its debug output on, as the autostart entry would start it once
# the screen reader setting is on.
{ lib, ... }:

{
  imports = [ ./wlGnomeLinda.nix ];
  services.orca.enable = lib.mkForce true;
}
