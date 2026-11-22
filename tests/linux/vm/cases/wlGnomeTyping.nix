# Typing, Remote Access and Linda's windows on GNOME: what wlGnomeLinda.nix gives its guest, and the
# Remote Access leader, the guest half of wlGnomeTyping.py, which speaks NVDA Remote's protocol through
# tests/linux/remotePeer.py.
{ pkgs, ... }:

{
  imports = [ ./wlGnomeLinda.nix ];
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-remote-leader" ''
      export GROUND_REMOTE_PEER=${../../remotePeer.py}
      exec ${pkgs.python3.interpreter} ${./wlGnomeTyping.py} "$@"
    '')
  ];
}
