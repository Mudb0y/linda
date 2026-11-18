# BRLTTY's Python binding for BrlAPI, alone. BRLTTY builds it for whatever
# python3 it is given, so it is built here against Linda's interpreter, and
# only the module and the client library it links are kept: the rest of
# BRLTTY's tree would bring its drivers, tables, Tcl and BlueZ into the
# closure for a client that needs none of them.
{
  lib,
  stdenv,
  python,
  toPythonModule,
  brltty,
  patchelf,
}:

let
  brlttyForPython = (brltty.override { python3 = python; }).overrideAttrs (old: {
    # The client's default authorization names BrlAPI's key file, which nixpkgs leaves in BRLTTY's
    # own tree, where nothing makes it, and which would bring that tree into the closure. Upstream's
    # /etc/brlapi.key is where BRLTTY keeps it on other systems and where NixOS's module makes it.
    postConfigure = (old.postConfigure or "") + ''
      for header in config.h Programs/brlapi.h; do
        substituteInPlace $header --replace-fail "#define BRLAPI_ETCDIR \"$out/etc\"" '#define BRLAPI_ETCDIR "/etc"'
      done
    '';
  });
in
toPythonModule (
  stdenv.mkDerivation {
    pname = "brlapi";
    # The binding's own version, which BRLTTY's release does not share.
    version = "0.8.8";

    dontUnpack = true;
    nativeBuildInputs = [ patchelf ];

    installPhase = ''
      runHook preInstall
      site=$out/${python.sitePackages}
      mkdir -p $site $out/lib
      cp -P ${brlttyForPython}/lib/libbrlapi.so* $out/lib/
      chmod u+w $out/lib/*
      cp ${brlttyForPython}/${python.sitePackages}/brlapi.*.so $site/
      cp -r ${brlttyForPython}/${python.sitePackages}/Brlapi-*.egg-info $site/
      chmod -R u+w $site
      for module in $site/brlapi.*.so; do
        patchelf --set-rpath "$out/lib:$(patchelf --print-rpath $module | sed 's|${brlttyForPython}/lib:||')" $module
      done
      runHook postInstall
    '';

    # The module would otherwise load a libbrlapi other than the one it was
    # built with, or none, without the build saying so.
    doInstallCheck = true;
    installCheckPhase = ''
      if grep -rl ${brlttyForPython} $out; then
        echo "these still name BRLTTY's tree, which would bring all of it into the closure" >&2
        exit 1
      fi
      PYTHONPATH=$out/${python.sitePackages} ${python.interpreter} -c 'import brlapi; brlapi.KEY_CMD_ROUTE'
    '';

    passthru.brltty = brlttyForPython;

    meta = {
      description = "Python binding for BrlAPI, the client interface of BRLTTY";
      homepage = "https://brltty.app";
      license = lib.licenses.lgpl21Plus;
    };
  }
)
