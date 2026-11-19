# Linda built by its Meson build, with prefix $out, in one of two shapes. With librariesOnly, the
# three native libraries and the key capture helper alone, which the dev shell names too, so that a
# change to the Python tree rebuilds neither them nor the shell. Otherwise the program, which installs those libraries from
# `libraries` when given and builds them itself when not, with the eSpeak NG fork NVDA pins, the CLDR
# data, the louis tables and openevv's driver handed to Meson, and each launcher wrapped for GTK and
# GObject introspection.
{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  cargo,
  rustc,
  rustPlatform,
  pipewire,
  gettext,
  makeWrapper,
  wrapGAppsHook3,
  gobject-introspection,
  at-spi2-core,
  gtk3,
  librsvg,
  icu,
  liblouis,
  strace,
  desktop-file-utils,
  appstream,
  python,
  cargoVendorDir,
  louisTables ? null,
  nvdaCldr ? null,
  espeak ? null,
  openevvDriver ? null,
  # The LINDA_ variables the dev shell sets, which Meson must have fixed to the same values.
  runtimeEnv ? { },
  librariesOnly ? false,
  # Whether to install the system service that runs the key capture helper, which the NixOS module
  # in keyd-module.nix turns on.
  keydService ? false,
  # A build with librariesOnly, whose libraries the program installs instead of building its own.
  libraries ? null,
}:

let
  buildsLibraries = librariesOnly || libraries == null;

  # Each program installed, with the arguments its launcher puts before the caller's.
  programs = {
    linda = [ ];
    linda-say = [ "say" ];
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

  # What the Meson build must have written into linuxPlatform/installPaths.py: the dev shell's
  # values, and the libraries where this package installed them.
  libDir = "${placeholder "out"}/lib/linda";
  installedPaths = runtimeEnv // {
    LINDA_VBUF_LIB = "${libDir}/libnvdaVbuf.so";
    LINDA_VBUF_ENGINE = "${libDir}/libbre_vbuf.so";
    LINDA_PWSINK_LIB = "${libDir}/libpwsink.so";
    LINDA_AUDIO_SINK = "pipewire";
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

  appDir = "$out/share/linda/source";

  # Only what each shape builds from, so that a change elsewhere does not rebuild it. Built from a
  # git flake, the flake's source holds only tracked files, so nothing generated in the checkout comes
  # in.
  librarySources = [
    ../engine/Cargo.toml
    ../engine/Cargo.lock
    ../engine/crates
    ../engine/vbuf
    ../nvdaHelper/vbufBase/storage.cpp
    ../nvdaHelper/vbufBase/storage.h
    ../nvdaHelper/vbufBase/utils.cpp
    ../nvdaHelper/vbufBase/utils.h
    ../nvdaHelper/common
    ../tools/cargo-libraries
  ];
  programSources = [
    ../copying.txt
    ../source
    ../linux
    ../user_docs
    ../tools/build-artifacts
    ../tools/install-tree
  ];
in
stdenv.mkDerivation {
  pname = if librariesOnly then "linda-libraries" else "linda";
  version = lib.concatMapStringsSep "." versionField [
    "year"
    "major"
    "minor"
  ];

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions (
      [
        ../meson.build
        ../meson.options
      ]
      ++ lib.optionals buildsLibraries librarySources
      ++ lib.optionals (!librariesOnly) programSources
      ++ lib.optional keydService ../linux/keyd
    );
  };

  # pwmon records a sink's monitor for the tests. It goes to an output of its own, so that the
  # program's package, which takes only the libraries, never holds a program that records sound.
  outputs = [ "out" ] ++ lib.optional librariesOnly "pwmon";

  mesonBuildType = "release";
  mesonFlags =
    [
      (lib.mesonBool "program" (!librariesOnly))
      (lib.mesonBool "keyd_service" keydService)
    ]
    ++ lib.optional buildsLibraries (lib.mesonOption "cargo_vendor_dir" "${cargoVendorDir}")
    ++ lib.optional (!buildsLibraries) (lib.mesonOption "prebuilt_libraries" "${libraries}/lib/linda")
    ++ lib.optionals (!librariesOnly) [
      (lib.mesonOption "cldr_data" "${nvdaCldr}")
      (lib.mesonOption "louis_tables" "${louisTables}")
      (lib.mesonOption "espeak_library" "${espeak}/lib/libespeak-ng.so.1")
      (lib.mesonOption "espeak_data" "${espeak}/share/espeak-ng-data")
      (lib.mesonOption "openevv_driver" "${openevvDriver}")
      # icu-uc's pkg-config file names the dev output, which holds no library.
      (lib.mesonOption "icu_lib_dir" "${lib.getLib icu}/lib")
    ];

  nativeBuildInputs =
    [
      meson
      ninja
      pkg-config
      python
    ]
    ++ lib.optionals buildsLibraries [
      cargo
      rustc
      # libclang, for the bindings pipewire-sys and libspa-sys generate.
      rustPlatform.bindgenHook
    ]
    ++ lib.optionals (!librariesOnly) [
      gettext
      makeWrapper
      gobject-introspection
      wrapGAppsHook3
    ];
  # As the dev shell has them: the typelibs gi loads, GTK's SVG loader and GTK's GSettings schemas,
  # which wrapGAppsHook3 gathers for the wrappers, and what Meson asks pkg-config for.
  buildInputs =
    lib.optionals buildsLibraries [ pipewire ]
    ++ lib.optionals (!librariesOnly) [
      at-spi2-core
      gtk3
      librsvg
      icu
      liblouis
    ];
  dontWrapGApps = true;

  # Meson's tests here are the engine's, of the ring, the position and the clock of the PipeWire
  # sink and of the key capture helper against its mocked devices, and the validation of the
  # desktop entry and the metadata. The lint needs the git history, which a Nix build has not got.
  doCheck = true;
  mesonCheckFlags =
    lib.optionals buildsLibraries [
      "--suite"
      "engine"
    ]
    ++ lib.optionals (!librariesOnly) [
      "--suite"
      "files"
    ];
  nativeCheckInputs = lib.optionals (!librariesOnly) [
    desktop-file-utils
    appstream
  ];

  postInstall = lib.optionalString librariesOnly ''
    install -Dm755 cargo-target/release/pwmon "$pwmon/bin/pwmon"
  '';

  # After wrapGAppsHook3 has gathered its arguments, and after the launchers' interpreter line has
  # been pointed at the store.
  postFixup = lib.optionalString (!librariesOnly) (
    lib.concatMapStrings (name: ''
      wrapProgram $out/bin/${name} \
        "''${gappsWrapperArgs[@]}" \
        ${lib.concatMapStringsSep " " (n: "--unset ${n}") callerVariablesCleared}
    '') (lib.attrNames programs)
  );

  # Each wrapper, started with every cleared variable set, runs its launcher, which has its exec
  # of Python failed by strace, which records the environment it was handed: so the check sees what
  # the program would get without running it. The paths Meson fixed are checked against the dev
  # shell's, and each library must be there, as one that is not is not loaded, and NVDA carries on
  # without what it gives.
  doInstallCheck = !librariesOnly;
  nativeInstallCheckInputs = [ strace ];
  installCheckPhase = ''
    runHook preInstallCheck
    for setting in ${lib.escapeShellArgs (lib.mapAttrsToList (n: v: ''"${n}": "${v}",'') installedPaths)}; do
      if ! grep -qF "$setting" ${appDir}/linuxPlatform/installPaths.py; then
        echo "error: linuxPlatform/installPaths.py does not hold $setting"
        exit 1
      fi
    done
    for library in ${lib.escapeShellArgs (lib.attrValues (lib.filterAttrs (n: _: lib.hasSuffix "_LIB" n || lib.hasSuffix "_ENGINE" n) installedPaths))}; do
      if [ ! -f "$library" ]; then
        echo "error: linuxPlatform/installPaths.py names $library, which is not a file"
        exit 1
      fi
    done
    # The key capture helper, which a system service of its own runs, beside the libraries.
    if [ ! -x $out/lib/linda/linda-keyd ]; then
      echo "error: lib/linda holds no linda-keyd"
      exit 1
    fi
    # Where documentationUtils looks for the Help menu's pages when NVDA runs from source.
    for page in userGuide keyCommands changes; do
      if [ ! -f "${appDir}/../user_docs/en/$page.html" ]; then
        echo "error: share/linda/user_docs/en holds no $page.html"
        exit 1
      fi
    done
    for program in ${lib.escapeShellArgs (lib.attrNames programs)}; do
      env ${lib.concatMapStringsSep " " (n: "${n}=/caller") callerVariablesCleared} \
        strace -f -v -s 65536 -e trace=execve -e inject=execve:error=ENOENT:when=3 -o execve.log \
        $out/bin/$program 2>/dev/null || true
      if ! grep -F 'execve("${python}/bin/python3' execve.log >python.log; then
        echo "error: $program does not run ${python}/bin/python3"
        exit 1
      fi
      if ! grep -qF "\"-I\", \"-B\", \"${appDir}/linda.py\"" python.log; then
        echo "error: $program does not start ${appDir}/linda.py with -I -B"
        exit 1
      fi
      for name in ${lib.escapeShellArgs callerVariablesCleared}; do
        if grep -qF "\"$name=" python.log; then
          echo "error: $program hands Python the caller's $name"
          exit 1
        fi
      done
    done
    runHook postInstallCheck
  '';

  passthru = { inherit programs; };

  meta = {
    description =
      if librariesOnly then
        "Linda's native libraries: NVDA's virtual buffer storage, the PipeWire sink and the web document renderer; and its key capture helper"
      else
        "NVDA, the screen reader, on Linux";
    license = lib.licenses.gpl2Plus;
    platforms = lib.platforms.linux;
  } // lib.optionalAttrs (!librariesOnly) { mainProgram = "linda"; };
}
