# libnvdaVbuf, the storage behind NVDA's virtual buffers on Linux: upstream's storage.cpp and
# utils.cpp from nvdaHelper/vbufBase, unchanged, with engine/vbuf's C interface, built by the same
# script a checkout builds it with.
{
  lib,
  stdenv,
}:

stdenv.mkDerivation {
  pname = "basedreader-vbuf";
  inherit ((lib.importTOML ../engine/Cargo.toml).workspace.package) version;

  # Only what the library compiles from, so that a change elsewhere in nvdaHelper or the engine
  # does not rebuild it.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../engine/vbuf
      ../nvdaHelper/vbufBase/storage.cpp
      ../nvdaHelper/vbufBase/storage.h
      ../nvdaHelper/vbufBase/utils.cpp
      ../nvdaHelper/vbufBase/utils.h
      ../nvdaHelper/common
    ];
  };

  buildPhase = ''
    runHook preBuild
    bash engine/vbuf/build.sh "$PWD/nvdaHelper" "$PWD/libnvdaVbuf.so"
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 libnvdaVbuf.so "$out/lib/libnvdaVbuf.so"
    runHook postInstall
  '';

  meta = {
    description = "NVDA's virtual buffer storage as a Linux library";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
  };
}
