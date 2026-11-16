# Basedreader as an installed program: NVDA's tree with what
# tools/build-artifacts generates from it, read-only in the store, and a
# wrapper for each program that runs the launcher with the flake's Python and
# names the native pieces the code loads by absolute path.
{
  lib,
  stdenvNoCC,
  gettext,
  makeWrapper,
  wrapGAppsHook3,
  gobject-introspection,
  at-spi2-core,
  gtk3,
  librsvg,
  strace,
  python,
  louisTables,
  nvdaCldr,
  # BASEDREADER_ variables the program reads, as the dev shell sets them.
  runtimeEnv,
  # The PipeWire sink's library. Without it linuxPlatform.audioOutput keeps
  # its own choice of sink.
  pwsink ? null,
}:

let
  # Each program installed, with the arguments it puts before the caller's.
  programs = {
    basedreader = [ ];
    basedreader-say = [ "say" ];
  };

  # A caller's values for these would load code from outside the package into
  # the screen reader: a host's alsa-lib or PipeWire's libjack through the
  # loader, or a desktop's event-sound GTK module, which plays sounds through
  # the sound server on its own. GTK finds a module the desktop's settings
  # name, as GNOME's settings daemon names canberra-gtk-module while event
  # sounds are on, only on GTK_PATH; its accessibility bridge it loads by
  # itself.
  callerVariablesCleared = [
    "LD_LIBRARY_PATH"
    "LD_PRELOAD"
    "LD_AUDIT"
    "PYTHONPATH"
    "PYTHONHOME"
    "GTK_PATH"
    "GTK_MODULES"
    "GTK3_MODULES"
  ];

  wrapperEnv =
    runtimeEnv
    // lib.optionalAttrs (pwsink != null) {
      BASEDREADER_AUDIO_SINK = "pipewire";
      BASEDREADER_PWSINK_LIB = "${pwsink}/lib/libpwsink.so";
    };

  # NVDA's release, from the lines source/buildVersion.py sets it with.
  versionLines = lib.splitString "\n" (builtins.readFile ../source/buildVersion.py);
  versionField =
    name:
    let
      prefix = "version_${name} = ";
    in
    lib.removePrefix prefix (
      lib.findFirst (lib.hasPrefix prefix) (throw "source/buildVersion.py sets no version_${name}") versionLines
    );

  appDir = "$out/share/basedreader/source";
in
stdenvNoCC.mkDerivation {
  pname = "basedreader";
  version = lib.concatMapStringsSep "." versionField [
    "year"
    "major"
    "minor"
  ];

  # Built from a git flake, the flake's source holds only tracked files, so
  # nothing generated in the checkout comes in.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../source
      ../tools/build-artifacts
    ];
  };

  nativeBuildInputs = [
    python
    gettext
    makeWrapper
    gobject-introspection
    wrapGAppsHook3
  ];
  # As the dev shell has them: the typelibs gi loads, GTK's SVG loader and
  # GTK's GSettings schemas, which wrapGAppsHook3 gathers for the wrappers.
  buildInputs = [
    at-spi2-core
    gtk3
    librsvg
  ];
  dontWrapGApps = true;

  env = {
    BASEDREADER_LOUIS_TABLES_DIR = "${louisTables}";
    BASEDREADER_NVDA_CLDR_DIR = "${nvdaCldr}";
  };

  buildPhase = ''
    runHook preBuild
    python3 tools/build-artifacts
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/share/basedreader
    cp -r source ${appDir}
    # The program runs with -B and the store is read-only, so modules are
    # compiled here or on every start.
    python3 -m compileall -q -j0 --invalidation-mode unchecked-hash ${appDir}
    runHook postInstall
  '';

  # After wrapGAppsHook3 has gathered its arguments.
  postFixup = lib.concatStrings (
    lib.mapAttrsToList (name: arguments: ''
      makeWrapper ${python}/bin/python3 $out/bin/${name} \
        "''${gappsWrapperArgs[@]}" \
        ${lib.concatStringsSep " " (lib.mapAttrsToList (n: v: "--set-default ${n} ${lib.escapeShellArg v}") wrapperEnv)} \
        ${lib.concatMapStringsSep " " (n: "--unset ${n}") callerVariablesCleared} \
        --add-flags "-I -B ${appDir}/basedreader.py" \
        ${lib.concatMapStrings (a: "--add-flags ${lib.escapeShellArg a} ") arguments}
    '') programs
  );

  # Each wrapper, started with every cleared variable set, has its exec of
  # Python failed by strace, which records the environment it was handed: so
  # the check sees what the program would get without running it.
  doInstallCheck = true;
  nativeInstallCheckInputs = [ strace ];
  installCheckPhase = ''
    runHook preInstallCheck
    for program in ${lib.escapeShellArgs (lib.attrNames programs)}; do
      env ${lib.concatMapStringsSep " " (n: "${n}=/caller") callerVariablesCleared} \
        strace -f -v -s 65536 -e trace=execve -e inject=execve:error=ENOENT:when=2 -o execve.log \
        $out/bin/$program 2>/dev/null || true
      if ! grep -F 'execve("${python}/bin/python3"' execve.log >python.log; then
        echo "error: $program does not run ${python}/bin/python3"
        exit 1
      fi
      for name in ${lib.escapeShellArgs callerVariablesCleared}; do
        if grep -qF "\"$name=" python.log; then
          echo "error: $program hands Python the caller's $name"
          exit 1
        fi
      done
      for setting in ${lib.escapeShellArgs (lib.mapAttrsToList (n: v: ''"${n}=${v}"'') wrapperEnv)}; do
        if ! grep -qF "$setting" python.log; then
          echo "error: $program does not hand Python $setting"
          exit 1
        fi
      done
    done
    runHook postInstallCheck
  '';

  passthru = { inherit programs; };

  meta = {
    description = "NVDA, the screen reader, on Linux";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
    mainProgram = "basedreader";
  };
}
