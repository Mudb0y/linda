# The helper, and cases/keydTyping.py's guest half, which types the keys left waiting as the
# session leaves the front.
{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-typist" ''
      exec ${pkgs.python3.interpreter} ${./keydTyping.py} "$@"
    '')
  ];
}
