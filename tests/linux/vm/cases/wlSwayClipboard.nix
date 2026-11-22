# wl/clipboardCase.py's guest half as clipboard-other, the native GTK 3 program beside Linda, with
# wl-clipboard and wayland-info.
{ pkgs, ... }:

let
  python = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);
  other = pkgs.stdenv.mkDerivation {
    name = "clipboard-other";
    dontUnpack = true;
    nativeBuildInputs = [
      pkgs.wrapGAppsHook3
      pkgs.gobject-introspection
    ];
    buildInputs = [
      python
      pkgs.gtk3
    ];
    installPhase = ''
      mkdir -p $out/bin
      makeWrapper ${python.interpreter} $out/bin/clipboard-other --add-flags "${../wl/clipboardCase.py} other"
    '';
  };
in
{
  environment.systemPackages = [
    other
    pkgs.wl-clipboard
    pkgs.wayland-utils
  ];
}
