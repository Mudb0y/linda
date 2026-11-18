{
  description = "Linda, NVDA brought to Linux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  # A nixpkgs revision whose ruff is the release upstream's pyproject.toml
  # pins, which is the one its pre-commit runs over every file a merge
  # brings in. Move it when upstream moves that pin; tests/linux/test_ruff.py
  # fails until the two agree.
  inputs.nixpkgs-ruff.url = "github:NixOS/nixpkgs/8b7dc2ca50dd99ca3af5af1436b06b5adb1e54c9";
  # openevv's source, for its engine library and its NVDA driver, by revision
  # so that a build anywhere compiles the same engine.
  inputs.openevv = {
    url = "github:Mudb0y/openevv/7ffd7549e985a46ac1dcb0264c95b9111cbde304";
    flake = false;
  };

  outputs = { self, nixpkgs, nixpkgs-ruff, openevv }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      lib = nixpkgs.lib;
      inherit (nixpkgs-ruff.legacyPackages.${system}) ruff;

      # NVDA pins Python 3.13. Staying on it keeps the imported code, and the
      # add-ons written against it, on the interpreter they were tested with
      # rather than on whatever python3 currently is.
      pythonInterpreter = pkgs.python313.override {
        self = pythonInterpreter;
        packageOverrides = import ./nix/python-packages.nix;
      };

      # wxPython is 4.3 where this NVDA release pins 4.2: under 4.2 on GTK a
      # list control reaches AT-SPI as an empty panel, so its items cannot be
      # read at all.
      python = pythonInterpreter.withPackages (ps: with ps; [
        bleak
        configobj
        cryptography
        dbus-fast
        detect-secrets
        diff-match-patch
        fast-diff-match-patch
        fuzzysearch
        louis
        lxml
        markdown
        mdx-truly-sane-lists
        nh3
        parameterized
        psutil
        pygobject3
        pymdown-extensions
        pyphen
        # The Qt 6 fixture's toolkit.
        pyqt6
        pyserial
        pytest
        # The X key source: XI2 events and grabs, and the XKB keymap that turns
        # them into characters.
        python-xlib
        regex
        requests
        rpyc
        schedule
        unittest-xml-reporting
        url-normalize
        wxpython_4_3
        xkbcommon
      ]);

      louisTables = pkgs.callPackage ./nix/louis-tables.nix { };
      cargoVendorDir = pkgs.rustPlatform.importCargoLock { lockFile = ./engine/Cargo.lock; };
      nvdaCldr = pkgs.callPackage ./nix/nvda-cldr.nix { };
      espeak = pkgs.callPackage ./nix/espeak-ng.nix { };

      # What git describe says of the locked revision, which a flake input
      # does not record; evaluation stops until it is set again for a new one.
      openevvVersion = "0.7-0-g7ffd754";
      openevvLibeci =
        assert lib.assertMsg (lib.hasSuffix openevv.shortRev openevvVersion)
          "openevvVersion is not of ${openevv.rev}: set it to what git describe says of that commit";
        pkgs.callPackage ./nix/openevv-libeci.nix {
          src = openevv;
          version = openevvVersion;
        };
      openevvDriver = pkgs.callPackage ./nix/openevv-driver.nix {
        src = openevv;
        version = openevvVersion;
        libeci = openevvLibeci;
      };

      # The LINDA_ variables naming the native libraries and data Linux
      # code loads, which the dev shell sets and the package's Meson build
      # fixes in linuxPlatform/installPaths.py, where its install check
      # holds them to these. The code finds them through these rather than
      # through the loader's search path, so that what it loads is exactly
      # what the flake pins.
      runtimeEnv = {
        LINDA_ICU_LIB_DIR = "${lib.getLib pkgs.icu}/lib";
        # ICU exports every C symbol with this suffix, as in ubrk_open_78.
        LINDA_ICU_VERSION_MAJOR = lib.versions.major pkgs.icu.version;
        LINDA_ESPEAK_LIB = "${espeak}/lib/libespeak-ng.so.1";
        LINDA_ESPEAK_DATA_DIR = "${espeak}/share/espeak-ng-data";
        # linuxPlatform.synthDriverPaths puts it on NVDA's driver search path.
        LINDA_OPENEVV_DRIVER_DIR = "${openevvDriver}";
      };

      # The Meson build's two shapes. The libraries, browse mode's storage and renderer and the
      # PipeWire sink, are a package of their own, which the dev shell names as well; the program
      # installs them from it, so that a change to the Python tree rebuilds neither them nor the shell.
      libraries = pkgs.callPackage ./nix/linda.nix {
        inherit python cargoVendorDir;
        librariesOnly = true;
      };
      linda = pkgs.callPackage ./nix/linda.nix {
        inherit
          python
          cargoVendorDir
          runtimeEnv
          louisTables
          nvdaCldr
          espeak
          libraries
          ;
        inherit openevvDriver;
      };
      app = name: description: {
        type = "app";
        program = "${linda}/bin/${name}";
        meta = { inherit description; };
      };

      bench = pkgs.rustPlatform.buildRustPackage {
        pname = "linda-bench";
        inherit ((lib.importTOML ./engine/Cargo.toml).workspace.package) version;
        src = lib.fileset.toSource {
          root = ./engine;
          fileset = lib.fileset.unions [
            ./engine/Cargo.toml
            ./engine/Cargo.lock
            ./engine/crates
          ];
        };
        cargoLock.lockFile = ./engine/Cargo.lock;
        cargoBuildFlags = [ "--package" "linda-bench" ];
        # The engine's tests speak D-Bus and refuse to run outside tests/linux/rig,
        # which a Nix build cannot enter; cargo test in engine/ runs them there.
        doCheck = false;
      };
    in
    {
      apps.${system} =
        {
          default = app "linda" "Start Linda";
        }
        // lib.optionalAttrs (linda.programs ? linda-say) {
          say = app "linda-say" "Speak a sentence through Linda's speech and audio, then exit";
        };

      packages.${system} = {
        default = linda;
        inherit linda;
        inherit (pythonInterpreter.pkgs)
          configobj
          fast-diff-match-patch
          fuzzysearch
          louis
          ;
        louis-tables = louisTables;
        nvda-cldr = nvdaCldr;
        linda-bench = bench;
        espeak-ng = espeak;
        openevv-libeci = openevvLibeci;
        openevv-driver = openevvDriver;
        linda-libraries = libraries;
      };

      devShells.${system}.default = pkgs.mkShell (runtimeEnv // {
        nativeBuildInputs = with pkgs; [
          python
          cargo
          rustc
          clippy
          rustfmt
          maturin
          # The pinned ruff: a let binding takes precedence over with.
          ruff
          pkg-config
          # The Meson build, which installs Linda as a distribution would, and
          # the validators of its desktop entry and metadata.
          meson
          ninja
          desktop-file-utils
          appstream
          gobject-introspection
          gettext
          dbus
          xvfb
          xprop
          firefox
          # Browse mode in Chromium and in programs built on it.
          chromium
          electron
          # The desktop the tests drive: a window manager, the programs that
          # put windows, keys and layouts on the rig's display, and MATE's own
          # programs and a Qt editor to read.
          marco
          xdotool
          wmctrl
          setxkbmap
          caja
          pluma
          mate-terminal
          mate-panel
          mate-notification-daemon
          libnotify
          featherpad
          # Another program's side of the clipboard, for the clipboard tests.
          xclip
          # tests/linux/rig builds its sandbox with this, and gives it the
          # internet on request through pasta, in a namespace it makes and
          # enters with util-linux and whose routes it sets with ip.
          bubblewrap
          passt
          iproute2
          util-linux
          git
          # libclang, for crates whose bindings bindgen generates.
          rustPlatform.bindgenHook
        ];

        # PipeWire's headers and pkg-config file, for crates that link
        # libpipewire, and those of the libraries the Meson build looks for. Named here rather than listed as an input, because
        # stdenv puts every input's bin directory on PATH, and so on the rig's
        # PATH, which is this shell's own. Tests reach PipeWire's programs
        # only through the two variables below, and a host without them gets
        # none from this shell.
        PKG_CONFIG_PATH = lib.makeSearchPath "lib/pkgconfig" (
          map lib.getDev (with pkgs; [ pipewire icu liblouis at-spi2-core ])
        );
        # For tests that start a private daemon and session manager inside the
        # rig.
        LINDA_PIPEWIRE_BIN = "${lib.getBin pkgs.pipewire}/bin";
        LINDA_WIREPLUMBER_BIN = "${lib.getBin pkgs.wireplumber}/bin";
        # The library the PipeWire sink loads, the same file the package's wrappers name, and the
        # recorder its tests listen with. The shell does not choose the sink: a run that wants
        # PipeWire sets LINDA_AUDIO_SINK itself.
        LINDA_PWSINK_LIB = "${libraries}/lib/linda/libpwsink.so";
        LINDA_PWMON = "${libraries.pwmon}/bin/pwmon";

        # setup.sh would put $out/lib first in every RUNPATH the linker
        # writes, and in a shell $out is outputs/out under the working
        # directory: a library built here would load whatever was dropped
        # there ahead of the store's copies.
        NIX_NO_SELF_RPATH = true;

        # Listed as build inputs so the gobject-introspection hook puts their
        # typelibs on GI_TYPELIB_PATH.
        buildInputs = with pkgs; [
          at-spi2-core
          gtk3
          gtk4
          # Gtk-4.0's typelib imports Graphene's, which gtk4 does not bring.
          graphene
          # Its gdk-pixbuf loader is how GTK draws its own symbolic icons.
          librsvg
        ];

        # Not on PATH in any package; tests/linux/rig starts it by hand so the
        # private accessibility bus never depends on D-Bus activation files
        # the host may or may not have.
        AT_SPI_BUS_LAUNCHER = "${pkgs.at-spi2-core}/libexec/at-spi-bus-launcher";

        # tools/build-artifacts takes the tables and the CLDR data from these.
        LINDA_LOUIS_TABLES_DIR = "${louisTables}";
        # The crates engine/Cargo.lock names, for a Meson build that must not fetch them.
        LINDA_CARGO_VENDOR_DIR = "${cargoVendorDir}";
        LINDA_NVDA_CLDR_DIR = "${nvdaCldr}";
        # tools/check-upstream-edits compares it with the release upstream's
        # include/liblouis submodule pins.
        LINDA_LIBLOUIS_VERSION = pkgs.liblouis.version;

        # Where tests/linux/bench/run finds the bench once nix build
        # .#linda-bench has built it. Stripped of its dependency on the
        # package, so that entering the shell never builds the engine.
        LINDA_BENCH_BIN = builtins.unsafeDiscardStringContext "${bench}/bin/linda-bench";

        # The Qt fixture sets QT_PLUGIN_PATH from it, so that a test needs no
        # rig env to give Qt its X platform plugin. PyQt6's own Qt.
        LINDA_QT6_PLUGIN_PATH = "${pkgs.qt6.qtbase}/${pkgs.qt6.qtbase.qtPluginPrefix}";

        # The terminal fixture's shell. The bash on PATH is stdenv's, built
        # without readline, so arrows and Control with them would type escape
        # sequences instead of editing the line.
        LINDA_BASH = "${pkgs.bashInteractive}/bin/bash";

        # Every locale, so that which ones the tests can set does not depend
        # on the ones the host happens to have built.
        LOCALE_ARCHIVE = "${pkgs.glibcLocales}/lib/locale/locale-archive";

        # The host session exports one pointing at its own alsa-lib and
        # PipeWire's libjack. Inherited, it would put host audio libraries
        # ahead of the pinned ones in every process started from here,
        # including those under the rig.
        shellHook = ''
          unset LD_LIBRARY_PATH
          # What this shell itself provides, without what nix develop
          # appended from the calling session. tests/linux/rig gives its
          # sandbox only these, so that nothing under test depends on the
          # host's desktop.
          export LINDA_RIG_PATH=''${PATH%"''${nix_saved_PATH:+:$nix_saved_PATH}"}
          export LINDA_RIG_XDG_DATA_DIRS=''${XDG_DATA_DIRS%"''${nix_saved_XDG_DATA_DIRS:+:$nix_saved_XDG_DATA_DIRS}"}
          # GTK's file and directory choosers abort without GSettings schemas
          # for themselves, which the outputs on XDG_DATA_DIRS lack, and warn
          # on standard error without hicolor, the icon theme every desktop
          # falls back on.
          LINDA_RIG_XDG_DATA_DIRS=$GSETTINGS_SCHEMAS_PATH:${pkgs.hicolor-icon-theme}/share:$LINDA_RIG_XDG_DATA_DIRS
          # Browse mode's libraries, as the package's wrappers name them. Set here rather than as
          # attributes, which nix develop would put over the caller's own: a run that names a
          # library built in the checkout keeps it.
          export LINDA_VBUF_LIB=''${LINDA_VBUF_LIB:-${libraries}/lib/linda/libnvdaVbuf.so}
          export LINDA_VBUF_ENGINE=''${LINDA_VBUF_ENGINE:-${libraries}/lib/linda/libbre_vbuf.so}
          # The search path the build inputs' hooks made, for tests/linux/test_mesonInstall.py, which
          # configures the Meson build inside the rig, where only LINDA_ variables get in.
          export LINDA_PKG_CONFIG_PATH=$PKG_CONFIG_PATH
        '';
      });
    };
}
