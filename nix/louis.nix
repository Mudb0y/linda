{
  buildPythonPackage,
  liblouis,
  setuptools,
  unittestCheckHook,
}:

# nixpkgs installs this binding only for its default python3 as part of
# liblouis itself. Building it on its own against the cached library gives the
# interpreter NVDA pins a proper package without rebuilding liblouis.
buildPythonPackage {
  pname = "louis";
  inherit (liblouis) version src;
  pyproject = true;

  sourceRoot = "${liblouis.src.name}/python";

  # The library is named by absolute path, as nixpkgs' liblouis does, so that
  # nothing depends on the dynamic loader's search path.
  postPatch = ''
    substitute pyproject.toml.in pyproject.toml \
      --replace-fail "@VERSION@" "${liblouis.version}"
    substitute louis/__init__.py.in louis/__init__.py \
      --replace-fail "###LIBLOUIS_SONAME###" "${liblouis}/lib/liblouis.so"
  '';

  build-system = [ setuptools ];

  nativeCheckInputs = [ unittestCheckHook ];
  unittestFlags = [
    "-s"
    "tests"
  ];

  pythonImportsCheck = [ "louis" ];

  meta = {
    description = "Python bindings for liblouis";
    inherit (liblouis.meta) homepage license;
  };
}
