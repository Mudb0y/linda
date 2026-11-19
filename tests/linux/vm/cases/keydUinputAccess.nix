# Steam's udev rules, which tag /dev/uinput for the user in front.
{
  imports = [ ../keyd.nix ];
  hardware.steam-hardware.enable = true;
}
