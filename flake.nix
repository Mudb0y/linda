{
  description = "Basedreader, NVDA brought to Linux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      lib = nixpkgs.lib;

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
        pyserial
        pytest
        regex
        requests
        rpyc
        schedule
        unittest-xml-reporting
        url-normalize
        wxpython_4_3
      ]);

      louisTables = pkgs.callPackage ./nix/louis-tables.nix { };
      nvdaCldr = pkgs.callPackage ./nix/nvda-cldr.nix { };
    in
    {
      packages.${system} = {
        inherit (pythonInterpreter.pkgs)
          configobj
          fast-diff-match-patch
          fuzzysearch
          louis
          ;
        louis-tables = louisTables;
        nvda-cldr = nvdaCldr;
      };

      devShells.${system}.default = pkgs.mkShell {
        nativeBuildInputs = with pkgs; [
          python
          cargo
          rustc
          clippy
          rustfmt
          maturin
          ruff
          pkg-config
          gobject-introspection
          gettext
          dbus
          xvfb
          xprop
          firefox
          # tests/linux/rig builds its sandbox with this.
          bubblewrap
          git
        ];

        # Listed as build inputs so the gobject-introspection hook puts their
        # typelibs on GI_TYPELIB_PATH.
        buildInputs = with pkgs; [
          at-spi2-core
          gtk3
          # Its gdk-pixbuf loader is how GTK draws its own symbolic icons.
          librsvg
        ];

        # Not on PATH in any package; tests/linux/rig starts it by hand so the
        # private accessibility bus never depends on D-Bus activation files
        # the host may or may not have.
        AT_SPI_BUS_LAUNCHER = "${pkgs.at-spi2-core}/libexec/at-spi-bus-launcher";

        # Linux code finds native libraries and data through these rather
        # than through the loader's search path, so that what it loads is
        # exactly what the flake pins.
        BASEDREADER_ICU_LIB_DIR = "${lib.getLib pkgs.icu}/lib";
        # ICU exports every C symbol with this suffix, as in ubrk_open_78.
        BASEDREADER_ICU_VERSION_MAJOR = lib.versions.major pkgs.icu.version;
        BASEDREADER_LOUIS_TABLES_DIR = "${louisTables}";
        BASEDREADER_NVDA_CLDR_DIR = "${nvdaCldr}";

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
          export BASEDREADER_RIG_PATH=''${PATH%"''${nix_saved_PATH:+:$nix_saved_PATH}"}
          export BASEDREADER_RIG_XDG_DATA_DIRS=''${XDG_DATA_DIRS%"''${nix_saved_XDG_DATA_DIRS:+:$nix_saved_XDG_DATA_DIRS}"}
        '';
      };
    };
}
