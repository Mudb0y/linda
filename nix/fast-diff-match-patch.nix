{
  lib,
  buildPythonPackage,
  fetchPypi,
  setuptools,
  unittestCheckHook,
}:

buildPythonPackage rec {
  pname = "fast-diff-match-patch";
  version = "2.1.0";
  pyproject = true;

  # The same sdist, by hash, that NVDA's uv.lock pins.
  src = fetchPypi {
    pname = "fast_diff_match_patch";
    inherit version;
    hash = "sha256-rEAte/8EqE82PMW/qUhQm8TvhELG691WTETcOWE77EA=";
  };

  # find_packages() would install the sdist's tests directory as a top-level
  # package named tests, which then competes with NVDA's own tests package.
  postPatch = ''
    substituteInPlace setup.py \
      --replace-fail "packages=find_packages()," "packages=[],"
  '';

  build-system = [ setuptools ];

  nativeCheckInputs = [ unittestCheckHook ];

  pythonImportsCheck = [ "fast_diff_match_patch" ];

  meta = {
    description = "C++ implementation of google-diff-match-patch for Python";
    homepage = "https://github.com/JoshData/fast_diff_match_patch";
    license = lib.licenses.asl20;
  };
}
