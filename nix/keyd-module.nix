# The NixOS module that runs linda-keyd, the helper that takes keys in the kernel for Linda, as the
# flake exports it in nixosModules.linda-keyd. Everything comes from what the Meson build installs
# with keyd_service, as a distribution's package would have it: the unit, which also gives the
# helper /dev/uinput, and the polkit action. Only the user, which NixOS makes from its own options
# rather than from sysusers.d, and the module load of uinput, which NixOS takes from
# boot.kernelModules rather than from a package, are said here again.
{
  config,
  lib,
  ...
}:

let
  cfg = config.services.linda-keyd;
in
{
  options.services.linda-keyd = {
    enable = lib.mkEnableOption "linda-keyd, which lets Linda take keys in the kernel, before any program has them, while Linda runs in the session in front";
    package = lib.mkOption {
      type = lib.types.package;
      description = "Linda's libraries package, built with the key capture service (keyd_service), whose unit and polkit action are installed.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.packages = [ cfg.package ];
    systemd.services.linda-keyd.wantedBy = [ "multi-user.target" ];
    # The polkit action, which polkit reads from the system profile's share/polkit-1.
    environment.systemPackages = [ cfg.package ];
    security.polkit.enable = true;
    boot.kernelModules = [ "uinput" ];
    users.users.linda-keyd = {
      isSystemUser = true;
      group = "linda-keyd";
      description = "Linda's key capture helper";
    };
    users.groups.linda-keyd = { };
  };
}
