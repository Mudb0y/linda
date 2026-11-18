{
  lib,
  buildPythonPackage,
  fetchPypi,
  setuptools,
  markdown,
}:

buildPythonPackage rec {
  pname = "mdx_gh_links";
  version = "0.4";
  pyproject = true;

  # The same sdist, by hash, that NVDA's uv.lock pins.
  src = fetchPypi {
    inherit pname version;
    hash = "sha256-QdWqwqsgFCWqChk3PECVt55eAV/az+g8OYGZ/lXKNoY=";
  };

  build-system = [ setuptools ];

  dependencies = [ markdown ];

  pythonImportsCheck = [ "mdx_gh_links" ];

  meta = {
    description = "Python-Markdown extension for shorthand links to GitHub users, issues and commits";
    homepage = "https://github.com/Python-Markdown/github-links";
    license = lib.licenses.bsd3;
  };
}
