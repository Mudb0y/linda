# The race keydPidfd.py runs in the guest, the case file itself as a program; and a polkit rule that
# decides nothing and logs, for each question linda-keyd puts, the unit polkit found for the process,
# which it looks up only by a pidfd it was given.
{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [
    (pkgs.writeScriptBin "ground-pidfd-race" (
      "#!${pkgs.python3}/bin/python3\n" + builtins.readFile ./keydPidfd.py
    ))
  ];
  security.polkit.extraConfig = ''
    polkit.addRule(function (action, subject) {
      if (action.id == "io.github.mudb0y.linda.keyd.capture") {
        polkit.log("ground: process " + subject.pid + " is in unit " + subject.system_unit);
      }
    });
  '';
}
