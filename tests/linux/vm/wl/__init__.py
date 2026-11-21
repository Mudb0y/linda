"""What the cases of the Wayland guests share, beyond ground.py: default.nix makes this directory the
package wl for every case whose name begins wl, so that a case says from wl import shellProbe. The
X cases are not given it, so a change here runs none of them again, save one to shellProbe.py, whose
guest half every guest's tools carry.
"""
