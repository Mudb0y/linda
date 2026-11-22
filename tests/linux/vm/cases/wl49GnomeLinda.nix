# What wlGnomeHelper and wlGnomeLinda each add to their guest, for the one boot that runs both; and
# wl50GnomeLinda's, which runs the same on GNOME 50.
{ lib, ... }:

{
  imports = [
    ./wlGnomeHelper.nix
    ./wlGnomeLinda.nix
  ];
  # The ibus module of NixOS 25.11 and 26.05, which GNOME turns on, exports GTK_IM_MODULE=ibus; the
  # pinned nixpkgs' no longer does. With it ibus hands each key it does not take back to the program
  # as a second event, which the test window records as a second key though the program acts on one.
  # GTK 3 on Wayland otherwise takes its own module, through the compositor, as in the GNOME 51 guest.
  environment.variables.GTK_IM_MODULE = lib.mkForce "wayland";
}
