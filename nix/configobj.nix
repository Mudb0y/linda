{
  configobj,
  fetchFromGitHub,
}:

# NVDA pins this unreleased revision: its config.featureFlag raises
# VdtParamError(name_or_msg=...), a keyword the 5.0.9 release does not have.
configobj.overridePythonAttrs (old: {
  version = "5.1.0-unstable-2024-11-28";

  src = fetchFromGitHub {
    owner = "DiffSK";
    repo = "configobj";
    rev = "9c8a0a80c767bf8a3d6493ed01df6c351bddca42";
    hash = "sha256-LJ61oXSgHIwpGTPEe+JM+5Nbj00auqDmtWHxJ9YPlVk=";
  };

  # This revision dropped Python 2 support and with it the use of six.
  dependencies = [ ];

  meta = old.meta // {
    changelog = "https://github.com/DiffSK/configobj/blob/9c8a0a80c767bf8a3d6493ed01df6c351bddca42/CHANGES.rst";
  };
})
