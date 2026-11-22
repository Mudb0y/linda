# The helper, and cases/keydLatePass.py's guest half, the client that answers each key within a
# millisecond, as Linda does, and late-passes a kept Alt.
{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-late-passer" ''
      exec ${pkgs.python3.interpreter} ${./keydLatePass.py} "$@"
    '')
  ];
}
