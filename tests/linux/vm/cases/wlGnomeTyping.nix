# Typing, Remote Access and Linda's windows on GNOME: what wlGnomeLinda.nix gives its guest, and the
# guest half of wlGnomeTyping.py: the Remote Access leader, which speaks NVDA Remote's protocol through
# tests/linux/remotePeer.py, and the watcher of windows created and activated, over AT-SPI.
{ pkgs, lib, ... }:

let
  python = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);
in
{
  imports = [ ./wlGnomeLinda.nix ];
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-typing-guest" ''
      export GI_TYPELIB_PATH=${
        lib.makeSearchPath "lib/girepository-1.0" [
          pkgs.glib.out
          pkgs.at-spi2-core
        ]
      }''${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}
      export GROUND_REMOTE_PEER=${../../remotePeer.py}
      exec ${python.interpreter} ${./wlGnomeTyping.py} "$@"
    '')
  ];
}
