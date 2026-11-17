# libbre_vbuf, which walks a web document over AT-SPI into the stream NVDA's virtual buffer is built
# from and watches it for changes, built from engine/ with the engine's lock file.
{
  lib,
  rustPlatform,
}:

let
  workspace = (lib.importTOML ../engine/Cargo.toml).workspace;
  builtMembers = [
    "crates/vbufengine"
    "crates/mirror"
  ];
  # Cargo reads the manifest of every member of the workspace and looks for the sources of each
  # target it declares, but builds only the library's crates. The others are given their manifests
  # and empty targets, so that the dev shell, which names this library, is not rebuilt for every
  # change to their code.
  otherMembers = lib.filter (member: !lib.elem member builtMembers) workspace.members;
  declaredBinaries =
    member:
    map (bin: bin.path) (lib.filter (bin: bin ? path) ((lib.importTOML (../engine + "/${member}/Cargo.toml")).bin or [ ]));
in
rustPlatform.buildRustPackage {
  pname = "basedreader-vbufengine";
  inherit (workspace.package) version;
  # Without engine/.cargo, whose runner starts every test binary inside tests/linux/rig, which a
  # Nix build cannot enter.
  src = lib.fileset.toSource {
    root = ../engine;
    fileset = lib.fileset.unions (
      [
        ../engine/Cargo.toml
        ../engine/Cargo.lock
      ]
      ++ map (member: ../engine + "/${member}") builtMembers
      ++ map (member: ../engine + "/${member}/Cargo.toml") otherMembers
    );
  };
  postPatch = lib.concatMapStrings (
    member:
    ''
      mkdir -p ${member}/src
      touch ${member}/src/lib.rs
    ''
    + lib.concatMapStrings (path: ''
      mkdir -p "$(dirname ${member}/${path})"
      echo 'fn main() {}' >${member}/${path}
    '') (declaredBinaries member)
  ) otherMembers;
  cargoLock.lockFile = ../engine/Cargo.lock;

  cargoBuildFlags = [
    "--package"
    "basedreader-vbufengine"
  ];
  # The library and the mirror it walks with speak D-Bus to real or fake applications, and their
  # tests refuse to run outside the rig.
  doCheck = false;

  meta = {
    description = "Basedreader's renderer of web documents for NVDA's virtual buffers";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
  };
}
