# The programs the test guest runs for the test script: the session runner, the fake keyboard, the
# key recorder, the test window, the key capture helper's client and what pokes at the helper from
# outside, each from guest/ under a name of its own, wrapped for GTK.
{
  stdenv,
  python3,
  wrapGAppsHook3,
  gobject-introspection,
  gtk3,
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
    ground-keyd-client = "keydClient.py";
    ground-poke = "poke.py";
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
    ${builtins.concatStringsSep "\n" (
      map (name: ''
        makeWrapper ${python.interpreter} $out/bin/${name} --add-flags $out/libexec/${programs.${name}}
      '') (builtins.attrNames programs)
    )}
    runHook postInstall
  '';
}
