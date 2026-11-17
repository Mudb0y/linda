# libpwsink, through which linuxPlatform.audioOutput.pipewireSink plays to PipeWire, built from
# engine/ with the engine's lock file. pwmon, which records a sink's monitor for the tests, goes to
# an output of its own, so that the program's package, which takes only lib/libpwsink.so from the
# default output, never holds a program that records sound.
{
  lib,
  rustPlatform,
  pkg-config,
  pipewire,
}:

let
  workspace = (lib.importTOML ../engine/Cargo.toml).workspace;
  # Cargo reads the manifest of every member of the workspace and looks for its sources, but builds
  # only the library's crates. The others are given their manifests and an empty library, so that
  # the dev shell, which names this library, is not rebuilt for every change to their code.
  otherMembers = lib.filter (member: !lib.hasPrefix "crates/pwsink" member) workspace.members;
in
rustPlatform.buildRustPackage {
  pname = "linda-pwsink";
  inherit (workspace.package) version;
  # Without engine/.cargo, whose runner starts every test binary inside tests/linux/rig, which a
  # Nix build cannot enter.
  src = lib.fileset.toSource {
    root = ../engine;
    fileset = lib.fileset.unions (
      [
        ../engine/Cargo.toml
        ../engine/Cargo.lock
        ../engine/crates/pwsink
      ]
      ++ map (member: ../engine + "/${member}/Cargo.toml") otherMembers
    );
  };
  postPatch = lib.concatMapStrings (member: ''
    mkdir -p ${member}/src
    touch ${member}/src/lib.rs
  '') otherMembers;
  cargoLock.lockFile = ../engine/Cargo.lock;

  outputs = [
    "out"
    "pwmon"
  ];

  cargoBuildFlags = [
    "--package"
    "linda-pwsink"
  ];
  # The library's own test connects to PipeWire and refuses to run outside the rig; the core's
  # tests, of the ring, the position and the clock, need no daemon.
  cargoTestFlags = [
    "--package"
    "linda-pwsink-core"
  ];

  nativeBuildInputs = [
    pkg-config
    # libclang, for the bindings pipewire-sys and libspa-sys generate.
    rustPlatform.bindgenHook
  ];
  buildInputs = [ pipewire ];

  postInstall = ''
    moveToOutput bin/pwmon "$pwmon"
  '';

  meta = {
    description = "Linda's PipeWire output library";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
  };
}
