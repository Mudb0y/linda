# The helper; notify-send; a Python with GLib's D-Bus, which runs the recorder the case hands it; and
# pkexec, with alice the administrator polkit asks for, so that it raises Plasma's authentication agent
# for her password.
{ pkgs, lib, ... }:

let
  python = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);
in
{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [
    pkgs.libnotify
    (pkgs.writeShellScriptBin "ground-shell-python" ''
      export GI_TYPELIB_PATH=${
        lib.makeSearchPath "lib/girepository-1.0" [
          pkgs.glib.out
          pkgs.gobject-introspection
        ]
      }
      exec ${python.interpreter} "$@"
    '')
  ];
  security.polkit = {
    adminIdentities = [ "unix-user:alice" ];
    enablePkexecWrapper = true;
  };
}
