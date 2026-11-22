# The helper, and a rule by which libinput, and so GNOME Shell, leaves the helper's virtual keyboard
# alone while /run/ground-ignore-virtual exists: a desktop that does not read the keyboard, for the
# light probe to find.
{ ... }:

{
  imports = [ ../keyd.nix ];
  services.udev.extraRules = ''
    SUBSYSTEM=="input", ATTRS{name}=="Linda virtual keyboard", TEST=="/run/ground-ignore-virtual", ENV{LIBINPUT_IGNORE_DEVICE}="1"
  '';
}
