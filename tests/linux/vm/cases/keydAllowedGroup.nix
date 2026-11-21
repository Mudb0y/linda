{
  imports = [ ../keyd.nix ];
  services.linda-keyd.allowedGroup = "linda";
  # The session's user starts outside it; the case adds her.
  users.groups.linda = { };
}
