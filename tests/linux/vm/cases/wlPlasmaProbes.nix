# The helper; guest/ledProbe.py as ground-led-probe, under a Python that has evdev for the probe
# keyboards and GLib's D-Bus for the plain process that asks for Orca's key-monitor name; and Orca,
# which the Plasma guest leaves out, for E12 to start beside that process.
{ pkgs, ... }:

let
  python = pkgs.python3.withPackages (ps: [
    ps.evdev
    ps.pygobject3
  ]);
in
{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-led-probe" ''
      export GI_TYPELIB_PATH=${pkgs.glib.out}/lib/girepository-1.0
      exec ${python.interpreter} ${../guest/ledProbe.py} "$@"
    '')
    pkgs.orca
  ];
}
