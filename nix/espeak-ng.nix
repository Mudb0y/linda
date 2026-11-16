# eSpeak NG built the way NVDA builds espeak.dll (nvdaHelper/espeak/sconscript):
# the commit NVDA pins as its include/espeak submodule, sonic from the commit
# it pins as include/sonic, Klatt and speechPlayer on, and no MBROLA, no
# asynchronous mode and no audio output of its own, so that the library has no
# code that could open a sound device. The *_emoji dictionary sources are
# removed before compiling, as NVDA removes them; the cmn, ru and yue extended
# lists are compiled in, as NVDA copies them in. tools/check-upstream-edits
# fails when an upstream merge moves either pin.
{
  lib,
  stdenv,
  fetchFromGitHub,
  cmake,
  ninja,
}:

let
  # Only the static library, with position-independent code, so that it is
  # linked into libespeak-ng and the result needs no libsonic at run time.
  # Built with optimisation, which sonic's own Makefile leaves out.
  sonic = stdenv.mkDerivation {
    pname = "sonic-nvda";
    version = "0-unstable-b93885dc";
    src = fetchFromGitHub {
      owner = "waywardgeek";
      repo = "sonic";
      rev = "b93885dcb70aae50c6f76b0fe4e0868f029a077e";
      hash = "sha256-kKMAjoLdC3y9vx7rw80+T1r/QZ2miBjJCIAOs3fm2dg=";
    };
    buildPhase = ''
      runHook preBuild
      $CC -O2 -fPIC -c sonic.c -o sonic.o
      $AR rcs libsonic.a sonic.o
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      install -Dm644 libsonic.a $out/lib/libsonic.a
      install -Dm644 sonic.h $out/include/sonic.h
      runHook postInstall
    '';
  };
in
stdenv.mkDerivation (finalAttrs: {
  pname = "espeak-ng-nvda";
  # Named after the commit it builds, which tests/linux/test_devShell.py
  # holds to upstream's pin through the library's path.
  version = "1.53.0-unstable-${lib.substring 0 8 finalAttrs.src.rev}";
  src = fetchFromGitHub {
    owner = "nvaccess";
    repo = "espeak-ng";
    rev = "b97de3cf2ad32c1d48c6bd36dfbf57a9c587fedc";
    hash = "sha256-8ST6QKlUqpnQEwBTS0SDefc+hZJhK8h6q4LgqwBFrpo=";
  };

  # Without it, a mark between a full stop and the next sentence is never
  # reported, so say all moves the caret a sentence late. Windows NVDA's
  # eSpeak has the same fault.
  patches = [ ./patches/espeak-keep-tags-after-full-stop.patch ];

  postPatch = ''
    rm -v dictsource/*_emoji
  '';

  nativeBuildInputs = [
    cmake
    ninja
  ];
  buildInputs = [ sonic ];

  cmakeFlags = [
    (lib.cmakeBool "BUILD_SHARED_LIBS" true)
    (lib.cmakeBool "USE_ASYNC" false)
    (lib.cmakeBool "USE_KLATT" true)
    (lib.cmakeBool "USE_LIBPCAUDIO" false)
    (lib.cmakeBool "USE_LIBSONIC" true)
    (lib.cmakeBool "USE_MBROLA" false)
    (lib.cmakeBool "USE_SPEECHPLAYER" true)
    (lib.cmakeBool "ENABLE_TESTS" false)
    (lib.cmakeBool "ESPEAK_BUILD_MANPAGES" false)
    (lib.cmakeBool "ESPEAK_COMPAT" false)
    "-DSONIC_LIB=${sonic}/lib/libsonic.a"
    "-DSONIC_INC=${sonic}/include"
  ];

  meta = {
    description = "eSpeak NG as NVDA builds it, without audio output";
    license = lib.licenses.gpl3Plus;
    platforms = lib.platforms.linux;
  };
})
