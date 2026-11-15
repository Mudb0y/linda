# Python packages NVDA pins that nixpkgs either lacks or carries at a
# different version, applied as packageOverrides so that every package in the
# set sees the same configobj.
self: super: {
  configobj = self.callPackage ./configobj.nix { inherit (super) configobj; };
  fast-diff-match-patch = self.callPackage ./fast-diff-match-patch.nix { };
  fuzzysearch = self.callPackage ./fuzzysearch.nix { };
  louis = self.callPackage ./louis.nix { };
}
