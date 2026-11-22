# linda-keyd as packages for Debian and Ubuntu, Fedora and Arch Linux. The helper is linked statically
# against musl, so that one binary runs whatever C library and library versions a distribution has.
# Meson's install with keyd_service lays the service's files out with each distribution's library
# directory, and nfpm packs them as packaging/nfpm.yaml describes. The reader's package and the NixOS
# module use none of this: they build the helper with the Meson build, as before.
{
  lib,
  pkgs,
  # Linda's version with the commit's date and short hash.
  version,
  # The commit's time, as the files' dates in each package.
  sourceDateEpoch,
}:

let
  static = pkgs.pkgsStatic;

  helper = static.rustPlatform.buildRustPackage {
    pname = "linda-keyd";
    inherit ((lib.importTOML ../engine/Cargo.toml).workspace.package) version;
    # Without engine/.cargo, whose runner would put the tests in the rig.
    src = lib.fileset.toSource {
      root = ../engine;
      fileset = lib.fileset.unions [
        ../engine/Cargo.toml
        ../engine/Cargo.lock
        ../engine/crates
      ];
    };
    cargoLock.lockFile = ../engine/Cargo.lock;
    cargoBuildFlags = [
      "--package"
      "linda-keyd"
    ];
    # Its logic, socket and watchdog against the mocked device layer, as the Meson build runs them.
    cargoTestFlags = [
      "--package"
      "linda-keyd"
    ];

    # Static: no program interpreter and no shared library named, so no distribution's loader or
    # libraries are asked for.
    doInstallCheck = true;
    nativeInstallCheckInputs = [ pkgs.binutils-unwrapped ];
    installCheckPhase = ''
      runHook preInstallCheck
      if readelf --program-headers $out/bin/linda-keyd | grep -q INTERP; then
        echo "error: linda-keyd names a program interpreter"
        exit 1
      fi
      if readelf --dynamic $out/bin/linda-keyd | grep -q NEEDED; then
        echo "error: linda-keyd needs shared libraries"
        exit 1
      fi
      runHook postInstallCheck
    '';

    meta = {
      description = "Linda's key capture helper, linked statically";
      license = lib.licenses.gpl2Plus;
      platforms = lib.platforms.linux;
      mainProgram = "linda-keyd";
    };
  };

  # Each format, with the library directory Meson takes by default on its distributions with prefix
  # /usr, where the Meson build puts the helper.
  formats = {
    deb = "lib/x86_64-linux-gnu";
    rpm = "lib64";
    archlinux = "lib";
  };

  mesonSource = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../meson.build
      ../meson.options
      ../linux/keyd
    ];
  };

  # meson.build installs the three libraries with the helper from prebuilt_libraries and wants all
  # four there; the libraries are empty stand-ins, taken out after the install.
  installed =
    libdir:
    pkgs.runCommand "linda-keyd-installed-${lib.replaceStrings [ "/" ] [ "-" ] libdir}"
      {
        nativeBuildInputs = [
          pkgs.meson
          pkgs.ninja
        ];
      }
      ''
        mkdir prebuilt
        touch prebuilt/libnvdaVbuf.so prebuilt/libpwsink.so prebuilt/libbre_vbuf.so
        cp ${helper}/bin/linda-keyd prebuilt/
        meson setup build ${mesonSource} --prefix=/usr --libdir=${libdir} \
          -Dprogram=false -Dkeyd_service=true -Dprebuilt_libraries=$PWD/prebuilt
        DESTDIR=$out meson install -C build --no-rebuild
        rm $out/usr/${libdir}/linda/lib*.so
      '';

  package =
    format: libdir:
    pkgs.runCommand "linda-keyd-${format}-${version}"
      {
        nativeBuildInputs = [ pkgs.nfpm ];
        passthru = {
          inherit version;
          files = installed libdir;
        };
      }
      ''
        cp -r ${../packaging} packaging
        chmod -R u+w packaging
        cd packaging
        mkdir $out
        SOURCE_DATE_EPOCH=${toString sourceDateEpoch} \
        LINDA_KEYD_VERSION=${version} \
        LINDA_KEYD_ROOT=${installed libdir} \
        LINDA_KEYD_LIBDIR=/usr/${libdir} \
          nfpm package --config nfpm.yaml --packager ${format} --target $out/
      '';
in
{
  inherit helper;
  deb = package "deb" formats.deb;
  rpm = package "rpm" formats.rpm;
  arch = package "archlinux" formats.archlinux;
}
