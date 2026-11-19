{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  # A document for browse mode.
  environment.systemPackages = [ pkgs.firefox ];
}
