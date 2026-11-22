"""GNOME 50, as NixOS 26.05 ships it: what wl49GnomeLinda runs on GNOME 49, here, as GNOME 50's half
of experiment E25.
"""

from typing import TYPE_CHECKING

import wl49GnomeLinda as older

if TYPE_CHECKING:
	from ground import Ground

SHELL_VERSION = "50.6"


def run(ground: "Ground") -> None:
	older.gnomeRun(ground, SHELL_VERSION)
