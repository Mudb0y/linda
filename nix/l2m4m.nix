{
  lib,
  buildPythonPackage,
  fetchPypi,
  setuptools,
  markdown,
  latex2mathml,
}:

buildPythonPackage rec {
  pname = "l2m4m";
  version = "1.0.4";
  pyproject = true;

  # The same sdist, by hash, that NVDA's uv.lock pins.
  src = fetchPypi {
    inherit pname version;
    hash = "sha256-f8RR7bKBYEweudb6YmqMzodOig02QcylgcILdUT1E5Y=";
  };

  build-system = [ setuptools ];

  dependencies = [
    markdown
    latex2mathml
  ];

  pythonImportsCheck = [ "l2m4m" ];

  meta = {
    description = "Python-Markdown extension that converts LaTeX to MathML";
    homepage = "https://gitlab.com/parcifal/l2m4m";
    license = lib.licenses.gpl3Only;
  };
}
