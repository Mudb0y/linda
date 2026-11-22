{
  fetchFromGitHub,
  python,
  rustPlatform,
}:
python.pkgs.toPythonModule (
  rustPlatform.buildRustPackage {
    pname = "mathcat-for-python";
    # Match the MathCAT backend bundled with this NVDA release.
    version = "0.7.2";

    src = fetchFromGitHub {
      owner = "daisy";
      repo = "MathCATForPython";
      rev = "d4fb316a208a8f2e90896fe2e0e49eb9835a8577";
      hash = "sha256-d0nAKLL5/9+mxlCaaeG57ENuPP32eAGr7knXcWt10W8=";
    };
    cargoLock.lockFile = ./mathcat-Cargo.lock;
    postPatch = "cp ${./mathcat-Cargo.lock} Cargo.lock";

    PYO3_PYTHON = "${python.interpreter}";

    installPhase = ''
      runHook preInstall
      sitePackages="$out/${python.sitePackages}"
      install -Dm755 target/*/release/liblibmathcat_py.so "$sitePackages/libmathcat_py.so"
      mkdir "$sitePackages/mathcatAssets"
      cp -r addon/globalPlugins/MathCAT/Rules "$sitePackages/mathcatAssets/"
      # MathCAT tries to extract rule archives in place whenever they exist.
      "${python.interpreter}" - "$sitePackages/mathcatAssets/Rules" <<'PY'
      from pathlib import Path
      import sys
      from zipfile import ZipFile

      for archive in Path(sys.argv[1]).rglob("*.zip"):
          with ZipFile(archive) as rules:
              rules.extractall(archive.parent)
          archive.unlink()
      PY
      runHook postInstall
    '';

    # A Python extension is linked by Python, so the Rust test executable cannot link it.
    doCheck = false;
    doInstallCheck = true;
    installCheckPhase = ''
      PYTHONPATH="$out/${python.sitePackages}" "${python.interpreter}" - <<'PY'
      from pathlib import Path
      import libmathcat_py

      libmathcat_py.SetRulesDir(str(Path(libmathcat_py.__file__).with_name("mathcatAssets") / "Rules"))
      libmathcat_py.SetMathML("<math><mn>2</mn><mo>+</mo><mn>3</mn></math>")
      assert "2 plus 3" in libmathcat_py.GetSpokenText()
      assert libmathcat_py.GetBraille("")
      PY
    '';
  }
)
