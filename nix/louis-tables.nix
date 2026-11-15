{
  lib,
  runCommand,
  liblouis,
}:

let
  # brailleTables/__tables.py names these in mixed case, which only resolves on
  # a case-insensitive filesystem; liblouis ships them in lower case.
  aliases = [
    "No-No-g0.utb"
    "No-No-g1.ctb"
    "No-No-g2.ctb"
    "No-No-g3.ctb"
    "Pt-Pt-g1.utb"
    "Pt-Pt-g2.ctb"
    "Se-Se.ctb"
  ];
  link = alias: ''
    target=${lib.toLower alias}
    if [ ! -e "$out/$target" ]; then
      echo "liblouis has no $target to alias as ${alias}" >&2
      exit 1
    fi
    if [ -e "$out/${alias}" ]; then
      echo "liblouis already has ${alias}; drop the alias" >&2
      exit 1
    fi
    ln -s "$target" "$out/${alias}"
  '';
in
runCommand "liblouis-tables-${liblouis.version}" { } ''
  mkdir "$out"
  ln -s ${liblouis}/share/liblouis/tables/* "$out/"
  ${lib.concatMapStrings link aliases}
''
