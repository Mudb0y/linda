# The programs the test guest runs for the test script: the session runner, the fake keyboard, the
# key recorder, the test windows, the key capture helper's client, what pokes at the helper from
# outside, the compositor's driver and the shell probe, each from guest/ (the probe from wl/) under a
# name of its own, wrapped for GTK 3.
{
  lib,
  stdenv,
  python3,
  wrapGAppsHook3,
  gobject-introspection,
  gtk3,
  gtk4,
  graphene,
  at-spi2-core,
}:

let
  python = python3.withPackages (ps: [
    ps.evdev
    ps.python-xlib
    ps.pygobject3
  ]);
  programs = {
    ground-session = "groundSession.py";
    ground-keyboard = "fakeKeyboard.py";
    ground-record = "keyRecorder.py";
    ground-window = "testWindow.py";
    ground-window4 = "testWindow4.py";
    ground-focus = "groundFocus.py";
    ground-shell-probe = "shellProbe.py";
    ground-keyd-client = "keydClient.py";
    ground-poke = "poke.py";
  };
  # GTK 4 for its window alone: the runner's environment, which everything a case starts inherits,
  # Linda among them, stays what it was before the GTK 4 window existed.
  extraFlags = {
    ground-window4 = lib.escapeShellArgs [
      "--prefix"
      "GI_TYPELIB_PATH"
      ":"
      (lib.makeSearchPath "lib/girepository-1.0" [
        gtk4
        graphene
      ])
      "--prefix"
      "XDG_DATA_DIRS"
      ":"
      "${gtk4}/share/gsettings-schemas/${gtk4.name}"
    ];
  };
in
stdenv.mkDerivation {
  name = "linda-ground-tools";
  src = ./guest;
  nativeBuildInputs = [
    wrapGAppsHook3
    gobject-introspection
  ];
  buildInputs = [
    python
    gtk3
    at-spi2-core
  ];
  installPhase = ''
    runHook preInstall
    mkdir -p $out/bin $out/libexec
    cp *.py $out/libexec/
    cp ${./wl/shellProbe.py} $out/libexec/shellProbe.py
    ${builtins.concatStringsSep "\n" (
      map (name: ''
        makeWrapper ${python.interpreter} $out/bin/${name} --add-flags $out/libexec/${programs.${name}} ${
          extraFlags.${name} or ""
        }
      '') (builtins.attrNames programs)
    )}
    runHook postInstall
  '';
}
