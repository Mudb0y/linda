# Linda's acceptance on GNOME: the helper; wl/lindaAcceptance.py's guest half, with T1's keymap dump
# tool and the fake keyboard it builds its pointer keyboard on; Firefox, for experiment E4; and a
# linda on every user's PATH, which GNOME's Alt+F2 dialog runs by that name, as it runs the installed
# one. That linda is the package under test with the test's own settings, log and synthesizer, so
# that the copy started from the dialog is heard and read as the first one is.
{
  pkgs,
  lib,
  guestPackages,
  ...
}:

let
  linda = guestPackages.linda;
  # The directory, log and transcript ground.py gives the copy it starts (LINDA_DIRECTORY).
  directory = "/tmp/ground/linda";
  python = pkgs.python313.withPackages (ps: [
    ps.evdev
    ps.pygobject3
    ps.pywayland
    ps.xkbcommon
  ]);
  acceptance = pkgs.writeShellScriptBin "ground-linda-acceptance" ''
    export GI_TYPELIB_PATH=${
      lib.makeSearchPath "lib/girepository-1.0" [
        pkgs.glib.out
        pkgs.at-spi2-core
      ]
    }''${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}
    # GNOME's settings are dconf's; without its module GSettings reads a file of its own.
    export GIO_EXTRA_MODULES=${pkgs.dconf.lib}/lib/gio/modules''${GIO_EXTRA_MODULES:+:$GIO_EXTRA_MODULES}
    export GROUND_LINDA_SOURCE=${linda}/share/linda/source
    export GROUND_KEYMAP_DUMP=${../../waylandKeymapDump.py}
    export GROUND_FAKE_KEYBOARD=${../guest/fakeKeyboard.py}
    exec ${python.interpreter} ${../wl/lindaAcceptance.py} "$@"
  '';
  lindaOnPath = pkgs.writeShellScriptBin "linda" ''
    echo $$ > ${directory}/startedFromPath.pid
    export LINDA_TEST_HOOKS_DIR=${linda}/share/linda/tests/linux/testHooks
    export LINDA_TRANSCRIPT_FILE=${directory}/transcript.jsonl
    export LINDA_TRANSCRIPT_MODE=immediate
    exec ${linda}/bin/linda -c ${directory}/config -f ${directory}/nvda.log --debug-logging "$@"
  '';
in
{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [
    acceptance
    lindaOnPath
    pkgs.firefox
    pkgs.iproute2
  ];
}
