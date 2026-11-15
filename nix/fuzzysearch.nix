{
  lib,
  buildPythonPackage,
  fetchPypi,
  setuptools,
  attrs,
}:

buildPythonPackage rec {
  pname = "fuzzysearch";
  version = "0.8.1";
  pyproject = true;

  # The same sdist, by hash, that NVDA's uv.lock pins.
  src = fetchPypi {
    inherit pname version;
    hash = "sha256-5fUJYsaxw9/GyM39XiYEg4yVyxARjkyrUY5SkungscY=";
  };

  build-system = [ setuptools ];

  dependencies = [ attrs ];

  # The sdist ships the test modules but not tests/compat.py and
  # tests/utils.py, which nearly all of them import.
  doCheck = false;

  # setup.py falls back to pure Python without a word when the C extensions
  # fail to build; importing them here makes that failure loud.
  pythonImportsCheck = [
    "fuzzysearch"
    "fuzzysearch._common"
    "fuzzysearch._substitutions_only"
  ];

  meta = {
    description = "Find approximate subsequence matches";
    homepage = "https://github.com/taleinat/fuzzysearch";
    license = lib.licenses.mit;
  };
}
