{
  imports = [ ../keyd.nix ];
  # The session's user signs in by herself on any text console switched to, so that a session of
  # hers other than the X one can be put in front.
  services.getty.autologinUser = "alice";
}
