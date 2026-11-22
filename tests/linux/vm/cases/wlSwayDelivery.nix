# The delivery watch in the sway guest: the helper, and the uhid keyboard it holds, which guestSway.nix
# gives the Polish layout as it gives the helper's virtual keyboard. Linda is the package under test,
# started by the case.
{ ... }:

{
  imports = [ ../keyd.nix ];
}
