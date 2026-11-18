# The key capture tests, each a NixOS test whose guest has a real kernel keyboard path: one flake
# check, vm-NAME, for each file NAME.py in cases/, and vm, which builds them all.
{
  pkgs,
  lib,
  linda,
  # Packages of the flake a case's module installs in its guest, by name.
  guestPackages ? { },
}:

let
  tools = pkgs.callPackage ./tools.nix { };

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

  check =
    name:
    pkgs.testers.runNixOSTest {
      name = "linda-keys-${name}";
      # A case that needs more in its guest, such as a service, says so in cases/NAME.nix, a NixOS
      # module that is given guestPackages.
      nodes.machine = {
        imports = [ (import ./guest.nix { inherit tools; }) ] ++ lib.optional (caseModules ? ${name}) caseModules.${name};
        _module.args.guestPackages = guestPackages // {
          linda = lindaWithHooks;
        };
      };
      # The driver's type check cannot follow ground, which the script loads from the store.
      skipTypeCheck = true;
      globalTimeout = 900;
      testScript = ''
        # Each file by its own path, so that a change to one case runs that case again and no other.
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location("ground", "${./ground.py}")
        ground = importlib.util.module_from_spec(spec)
        sys.modules["ground"] = ground
        spec.loader.exec_module(ground)
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
  vm = pkgs.linkFarm "linda-keys-vm" checks;
  linda-with-test-hooks = lindaWithHooks;
}
