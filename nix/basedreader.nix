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
  };

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
        --unset LD_LIBRARY_PATH --unset PYTHONPATH --unset PYTHONHOME \
        --add-flags "-I -B ${appDir}/basedreader.py" \
        ${lib.concatMapStrings (a: "--add-flags ${lib.escapeShellArg a} ") arguments}
    '') programs
  );

  passthru = { inherit programs; };

  meta = {
    description = "NVDA, the screen reader, on Linux";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
    mainProgram = "basedreader";
  };
}
