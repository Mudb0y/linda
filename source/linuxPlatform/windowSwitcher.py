"""The window marco's switcher named last and the window that came to the front last, so that a
window's title is said once as Alt+Tab brings it to the front.

The switcher names each window Alt+Tab chooses while Alt is held, some 70 ms before releasing Alt
activates it, and the window's title is then said again as NVDA's focus enters it. NVDA on Windows
cancels speech as the foreground changes, which here cuts the switcher's naming of that same window
off after a word or two; so the window it named comes to the front without cancelling it, and without
its title said again. A busy marco's naming can also be heard only after the window is in front.
"""

import time

#: Seconds after the switcher named a window during which that window coming to the front is the switch
#: it was named for: Alt is let go about a tenth of a second after the name, or after listening to it.
NAMED_SECONDS = 3.0
#: Seconds after a window came to the front during which the switcher naming it is late for that switch.
LATE_SECONDS = 1.0

#: The name the switcher said last, when, and how many speech sequences had been spoken with it.
_named: tuple[str, float, int] | None = None
#: Speech sequences spoken since the switcher first named a window.
_spoken = 0
_counting = False
#: The window that came to the front last, and when.
_front: tuple[str, float] | None = None


def _countSpeech(**kwargs) -> None:
	global _spoken
	_spoken += 1


def noteNamed(name: str) -> None:
	"""Remember that the switcher has just said name."""
	global _named, _counting
	if not _counting:
		from speech.extensions import pre_speech

		pre_speech.register(_countSpeech)
		_counting = True
	_named = (name, time.monotonic(), _spoken)


def namedJustNow(name: str) -> bool:
	"""Whether the switcher's naming of a window named name is the last thing spoken, and recent."""
	if _named is None or not name:
		return False
	named, at, spoken = _named
	return named == name and spoken == _spoken and time.monotonic() - at < NAMED_SECONDS


def noteFront(name: str) -> None:
	"""Remember that a window named name has just come to the front."""
	global _front
	_front = (name, time.monotonic())


def cameToFrontJustNow(name: str) -> bool:
	"""Whether the window that came to the front last is named name, and came within LATE_SECONDS."""
	return _front is not None and _front[0] == name and time.monotonic() - _front[1] < LATE_SECONDS
