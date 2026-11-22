"""Linda's copy and paste on KDE Plasma, beside a native GTK 3 program that has the keyboard, with
Klipper taking each copy over: through ext_data_control_manager_v1, with no window of Linda's, as
wl/clipboardCase.py holds it.
"""

from typing import TYPE_CHECKING

from wl import clipboardCase

if TYPE_CHECKING:
	from ground import Ground


def run(ground: "Ground") -> None:
	clipboardCase.run(ground)
