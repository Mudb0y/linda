# libeci.so.1 from the OpenEVV tree: the ECI library the openevv NVDA driver loads.
# Built the way OpenEVV's own CI builds what it ships (.github/workflows/build.yml,
# "Build what ships"): rules compiled to C, every language in one library, plain name.
{ lib, stdenv, python3, src, version ? "unstable"
, languages ? [ "enus" "engb" "dede" "eses" "esus" "frfr" "frca" "itit" "plpl" "jajp" "fifi" "ptbr" ]
, rules ? "c"
}:
stdenv.mkDerivation {
  pname = "openevv-libeci";
  inherit version src;

  # The Makefile writes each language's rules out of lang/<tag>/rules with Python.
  nativeBuildInputs = [ python3 ];
  enableParallelBuilding = true;

  # LANGS holds spaces, which plain makeFlags would split.
  preBuild = ''
    makeFlagsArray+=(
      "LANGS=${lib.concatMapStringsSep " " (t: "lang/${t}") languages}"
      "RULES=${rules}"
      "EVVPLAIN=1"
      "CCACHE="
      "PREFIX=$out"
    )
  '';
  buildFlags = [ "so" ];
  installTargets = [ "install-lib" ];

  meta.platforms = [ "x86_64-linux" "aarch64-linux" ];
}
