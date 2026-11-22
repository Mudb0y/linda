"""Plasma 6.6, as NixOS 26.05 ships it: what wl65PlasmaLinda runs on Plasma 6.5, here, as Plasma 6.6's
half of experiment E25.
"""

from typing import TYPE_CHECKING

import wl65PlasmaLinda as older

if TYPE_CHECKING:
	from ground import Ground

KWIN_VERSION = "6.6.6"


def run(ground: "Ground") -> None:
	older.plasmaRun(ground, KWIN_VERSION)
