{
  description = "Basedreader, NVDA brought to Linux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      # NVDA pins Python 3.13. Staying on it keeps the imported code, and the
      # add-ons written against it, on the interpreter they were tested with
      # rather than on whatever python3 currently is. wxPython is 4.3 where
      # this NVDA release pins 4.2: under 4.2 on GTK a list control reaches
      # AT-SPI as an empty panel, so its items cannot be read at all.
      python = pkgs.python313.withPackages (ps: with ps; [
        bleak
        configobj
        cryptography
        dbus-fast
        detect-secrets
        diff-match-patch
        lxml
        markdown
        mdx-truly-sane-lists
        nh3
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
        url-normalize
        wxpython_4_3
      ]);
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        nativeBuildInputs = with pkgs; [
          python
          cargo
          rustc
          clippy
          rustfmt
          pkg-config
          gobject-introspection
          dbus
          xvfb-run
          xprop
          firefox
        ];

        # Listed as build inputs so the gobject-introspection hook puts their
        # typelibs on GI_TYPELIB_PATH.
        buildInputs = with pkgs; [
          at-spi2-core
          gtk3
        ];

        # Not on PATH in any package; tests/linux/rig starts it by hand so the
        # private accessibility bus never depends on D-Bus activation files
        # the host may or may not have.
        AT_SPI_BUS_LAUNCHER = "${pkgs.at-spi2-core}/libexec/at-spi-bus-launcher";
      };
    };
}
