{
  lib,
  buildPythonPackage,
  fetchPypi,
  setuptools,
  markdown,
}:

buildPythonPackage rec {
  pname = "markdown_link_attr_modifier";
  version = "0.2.1";
  pyproject = true;

  # The same sdist, by hash, that NVDA's uv.lock pins.
  src = fetchPypi {
    inherit pname version;
    hash = "sha256-GN9Jqf57XIfa1Qt1wqIpmuQMZWdPexJj+xJFX133rJk=";
  };

  build-system = [ setuptools ];

  dependencies = [ markdown ];

  pythonImportsCheck = [ "markdown_link_attr_modifier" ];

  meta = {
    description = "Python-Markdown extension that sets the attributes of links";
    homepage = "https://github.com/Phuker/markdown_link_attr_modifier";
    license = lib.licenses.gpl3Only;
  };
}
