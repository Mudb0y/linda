# Python packages NVDA pins that nixpkgs either lacks or carries at a
# different version, applied as packageOverrides so that every package in the
# set sees the same configobj.
self: super: {
  configobj = self.callPackage ./configobj.nix { inherit (super) configobj; };
  fast-diff-match-patch = self.callPackage ./fast-diff-match-patch.nix { };
  fuzzysearch = self.callPackage ./fuzzysearch.nix { };
  l2m4m = self.callPackage ./l2m4m.nix { };
  louis = self.callPackage ./louis.nix { };
  markdown-link-attr-modifier = self.callPackage ./markdown-link-attr-modifier.nix { };
  mdx-gh-links = self.callPackage ./mdx-gh-links.nix { };
}
