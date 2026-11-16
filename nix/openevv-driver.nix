# openevv's NVDA synthesizer driver, laid out as its add-on is on Windows: the
# driver's three modules beside openevv_engine/, which holds the engine
# library, so that the driver finds the library where it looks on every
# platform and reads no variable of Basedreader's. The series in
# patches/openevv, applied in file-name order, makes the driver load on Linux
# and fixes its cancelling, marks and settings.
{
  lib,
  stdenvNoCC,
  src,
  version,
  libeci,
}:

let
  patchDir = ./patches/openevv;
  patchNames = lib.sort lib.lessThan (
    lib.attrNames (
      lib.filterAttrs (name: type: type == "regular" && lib.hasSuffix ".patch" name) (
        builtins.readDir patchDir
      )
    )
  );
in
stdenvNoCC.mkDerivation {
  pname = "openevv-driver";
  inherit src version;

  patches = map (name: patchDir + "/${name}") patchNames;

  dontConfigure = true;
  dontBuild = true;
  # The modules are installed as the patches leave them.
  dontFixup = true;

  installPhase = ''
    runHook preInstall
    mkdir -p $out/openevv_engine
    cp nvda/addon/synthDrivers/{openevv,_openevv,_openevv_dictionaries}.py $out/
    ln -s ${libeci}/lib/libeci.so.1 $out/openevv_engine/libeci.so.1
    runHook postInstall
  '';

  meta.platforms = [ "x86_64-linux" ];
}
