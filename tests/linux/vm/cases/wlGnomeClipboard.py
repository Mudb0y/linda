"""Linda's copy and paste on GNOME, beside a native GTK 3 program that has the keyboard: with no
data control there, through a window of Linda's own that has the keyboard for the moment, as
wl/clipboardCase.py holds it.
"""

from typing import TYPE_CHECKING

from wl import clipboardCase

if TYPE_CHECKING:
	from ground import Ground


def run(ground: "Ground") -> None:
	clipboardCase.run(ground)
