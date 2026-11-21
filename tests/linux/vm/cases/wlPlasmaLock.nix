# The helper, and a document for browse mode below the lock. The guest's kscreenlockerrc already keeps
# the locker from locking by itself.
{ pkgs, ... }:

{
  imports = [ ../keyd.nix ];
  environment.systemPackages = [ pkgs.firefox ];
}
