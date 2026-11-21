# The helper; the kernel's SysRq handler on, so that a typed key it would take as a command is seen
# to be refused and the user's own is seen to reach it; and cases/keydTyping.py's guest half, the
# client that types faster than the test client can.
{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  boot.kernel.sysctl."kernel.sysrq" = 1;
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-typist" ''
      exec ${pkgs.python3.interpreter} ${./keydTyping.py} "$@"
    '')
  ];
}
