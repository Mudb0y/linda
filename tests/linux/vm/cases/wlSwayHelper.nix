# The helper, and wl/helperSafety.py's guest half, which watches for the compositor to open the
# virtual keyboard.
{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-helper-safety" ''
      exec ${pkgs.python3.interpreter} ${../wl/helperSafety.py} "$@"
    '')
  ];
}
