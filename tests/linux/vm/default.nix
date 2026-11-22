# The key capture tests, each a NixOS test whose guest has a real kernel keyboard path: one flake
# check, vm-NAME, for each file NAME.py in cases/; vm, which builds every case of the X guest;
# vm-wayland, which builds every case of a Wayland guest from this nixpkgs; and vm-wayland-older,
# which builds the cases of the older desktops, each from the stable release that ships it.
{
  pkgs,
  lib,
  linda,
  # The flake's NixOS module for linda-keyd, in every guest; a case turns the helper on in its own
  # module, through keyd.nix.
  keydModule ? { },
  # Packages of the flake a case's module installs in its guest, by name.
  guestPackages ? { },
  # The legacyPackages of other nixpkgs releases, by release, which the older desktops' guests and
  # their test drivers are built from.
  releases ? { },
}:

let
  tools = pkgs.callPackage ./tools.nix { };

  # A case whose name begins with one of these boots that version of a desktop, built from the
  # release that ships it, in the guest its name less the version names: wl49GnomeLinda boots GNOME
  # 49 in the guest a wlGnome case boots. Only the package under test and the helper stay this
  # nixpkgs' own, as a user of that release would install them from this flake.
  olderDesktops = {
    wl49 = "25.11";
    wl50 = "26.05";
    wl65 = "25.11";
    wl66 = "26.05";
  };
  # 25.11 has no Plasma Login Manager, so the older Plasma guests sign in through SDDM.
  olderGuests = {
    wlGnome = ./guestGnome.nix;
    wlPlasma = ./guestPlasmaOld.nix;
  };
  olderPrefix = name: lib.findFirst (prefix: lib.hasPrefix prefix name) null (lib.attrNames olderDesktops);
  isOlder = name: olderPrefix name != null;
  # The case's name less its version, which chooses its guest.
  currentName = name: if isOlder name then "wl" + lib.removePrefix (olderPrefix name) name else name;
  pkgsOf =
    name:
    if !isOlder name then
      pkgs
    else
      releases.${olderDesktops.${olderPrefix name}}
        or (throw "the case ${name} needs nixpkgs ${olderDesktops.${olderPrefix name}}, which the flake does not give tests/linux/vm");
  # The guest's own programs come from its release, so that they run on the desktop's own libraries.
  toolsOf =
    name:
    let
      release = pkgsOf name;
    in
    if isOlder name then
      release.callPackage ./tools.nix {
        # The releases name the X library xlib, which nixpkgs has since renamed python-xlib.
        python3 = release.python3.override {
          packageOverrides = _: super: { python-xlib = super.python-xlib or super.xlib; };
        };
      }
    else
      tools;

  # The package as it is, with the test hooks laid beside its source as they are in a checkout, so
  # that LINDA_TEST_HOOKS_DIR gives it the transcript synth as it gives the tests under tests/linux
  # (source/linuxPlatform/testHooks.py honours the variable only then). Nothing is built again: the
  # source is linked, and only the launchers are copied, with the package's own path in them made
  # this one's, which has the same length because the name is the same.
  lindaWithHooks = pkgs.runCommand linda.name { nativeBuildInputs = [ pkgs.python3 ]; } ''
    hooks=$out/share/linda/tests/linux/testHooks
    mkdir -p $out/bin $hooks
    ln -s ${linda}/lib $out/lib
    for entry in ${linda}/share/*; do
      [ "''${entry##*/}" = linda ] || ln -s "$entry" $out/share/
    done
    for entry in ${linda}/share/linda/*; do
      ln -s "$entry" $out/share/linda/
    done
    cp -r ${../synthDrivers} $hooks/synthDrivers
    cp -r ${../fixtures/globalPlugins} $hooks/globalPlugins
    for launcher in ${linda}/bin/* ${linda}/bin/.*-wrapped; do
      python3 - "$launcher" $out/bin/''${launcher##*/} ${linda} $out <<'EOF'
    import os, sys
    source, target, old, new = sys.argv[1:]
    assert len(old) == len(new), (old, new)
    data = open(source, "rb").read()
    assert old.encode() in data, f"{source} does not name {old}"
    open(target, "wb").write(data.replace(old.encode(), new.encode()))
    os.chmod(target, 0o555)
    EOF
    done
  '';

  caseFiles = lib.attrNames (builtins.readDir ./cases);
  caseNames = map (lib.removeSuffix ".py") (lib.filter (lib.hasSuffix ".py") caseFiles);
  caseModules = lib.genAttrs (map (lib.removeSuffix ".nix") (lib.filter (lib.hasSuffix ".nix") caseFiles)) (
    name: ./cases + "/${name}.nix"
  );

  # A case's guest is chosen by its name: one beginning wl boots the Wayland guest its prefix names,
  # and every other case boots guest.nix, the X session.
  waylandGuests = {
    wlGnome = ./guestGnome.nix;
    wlSway = ./guestSway.nix;
    wlPlasma = ./guestPlasma.nix;
  };
  isWayland = lib.hasPrefix "wl";
  guestOf =
    name:
    let
      guests = if isOlder name then olderGuests else waylandGuests;
      prefix = lib.findFirst (prefix: lib.hasPrefix prefix (currentName name)) null (lib.attrNames guests);
    in
    if !isWayland name then
      ./guest.nix
    else if prefix != null && builtins.pathExists guests.${prefix} then
      guests.${prefix}
    else
      throw "the case ${name} names no Wayland guest that exists: its name must begin with one of ${lib.concatStringsSep ", " (lib.attrNames guests)}${lib.optionalString (isOlder name) " after its version"}, and that guest's file must be written";

  # An older desktop's case runs the subtests the current desktop's cases have, so it may import
  # those cases by name: wlGnomeLinda's, and the older cases' own, which wl49GnomeLinda and, for
  # Plasma, wl65PlasmaLinda hold. Each is given by its own path, as the case itself is, so that a
  # change to any other case runs it no more.
  olderImports = {
    wl49GnomeLinda = [ "wlGnomeLinda" ];
    wl50GnomeLinda = [
      "wlGnomeLinda"
      "wl49GnomeLinda"
    ];
    wl65PlasmaLinda = [
      "wlGnomeLinda"
      "wl49GnomeLinda"
    ];
    wl66PlasmaLinda = [
      "wlGnomeLinda"
      "wl49GnomeLinda"
      "wl65PlasmaLinda"
    ];
  };
  loadCases =
    name:
    lib.concatMapStrings (imported: ''
      spec = importlib.util.spec_from_file_location("${imported}", "${./cases + "/${imported}.py"}")
      sys.modules["${imported}"] = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(sys.modules["${imported}"])
    '') (olderImports.${name} or [ ]);

  # The Wayland cases' shared code, wl/, as the package wl.
  loadWayland = ''
    spec = importlib.util.spec_from_file_location(
        "wl", "${./wl}/__init__.py", submodule_search_locations=["${./wl}"]
    )
    wl = importlib.util.module_from_spec(spec)
    sys.modules["wl"] = wl
    spec.loader.exec_module(wl)
  '';

  check =
    name:
    (pkgsOf name).testers.runNixOSTest {
      name = "linda-keys-${name}";
      # A case that needs more in its guest, such as a service, says so in cases/NAME.nix, a NixOS
      # module that is given guestPackages.
      nodes.machine = {
        imports = [
          (import (guestOf name) { tools = toolsOf name; })
          keydModule
        ]
        ++ lib.optional (caseModules ? ${name}) caseModules.${name};
        _module.args.guestPackages = guestPackages // {
          linda = lindaWithHooks;
        };
      };
      # The driver's type check cannot follow ground, which the script loads from the store.
      skipTypeCheck = true;
      # A desktop shell's guest takes minutes longer to boot than the X one.
      # An older desktop's case runs the helper's safety suite and then Linda's acceptance in one.
      globalTimeout =
        if isOlder name then
          2400
        else if isWayland name then
          1500
        else
          900;
      testScript = ''
        # Each file by its own path, so that a change to one case runs that case again and no other.
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("ground", "${./ground.py}")
        ground = importlib.util.module_from_spec(spec)
        sys.modules["ground"] = ground
        spec.loader.exec_module(ground)
      ''
      + lib.optionalString (isWayland name) loadWayland
      + loadCases name
      + ''
        ground.main(
            machine,
            subtest,
            "${./cases + "/${name}.py"}",
            "${lindaWithHooks}",
            "${../synthDrivers/_transcriptRecords.py}",
            "${../harness.py}",
        )
      '';
    };

  checks = lib.genAttrs caseNames check;
in
lib.mapAttrs' (name: value: lib.nameValuePair "vm-${name}" value) checks
// {
  vm = pkgs.linkFarm "linda-keys-vm" (lib.filterAttrs (name: _: !isWayland name) checks);
  vm-wayland = pkgs.linkFarm "linda-keys-vm-wayland" (
    lib.filterAttrs (name: _: isWayland name && !isOlder name) checks
  );
  vm-wayland-older = pkgs.linkFarm "linda-keys-vm-wayland-older" (lib.filterAttrs (name: _: isOlder name) checks);
  linda-with-test-hooks = lindaWithHooks;
}
