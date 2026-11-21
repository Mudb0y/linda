# guest/ledProbe.py as ground-led-probe, under a Python that has evdev for the probe keyboards.
{ pkgs, ... }:

let
  python = pkgs.python3.withPackages (ps: [ ps.evdev ]);
in
{
  environment.systemPackages = [
    (pkgs.writeShellScriptBin "ground-led-probe" ''
      exec ${python.interpreter} ${../guest/ledProbe.py} "$@"
    '')
  ];
}
