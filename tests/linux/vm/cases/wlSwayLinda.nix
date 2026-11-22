# Linda's acceptance on sway: what wlGnomeLinda.nix gives its guest (the helper, wl/lindaAcceptance.py's
# guest half and the case's linda on PATH), and foot, the terminal in which the case types
# linda --take-keyboard after the emergency chord, as Linda's line on sway says to.
{ pkgs, ... }:

{
  imports = [ ./wlGnomeLinda.nix ];
  environment.systemPackages = [ pkgs.foot ];
}
