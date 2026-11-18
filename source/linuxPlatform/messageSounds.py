"""The sound of one of NVDA's message boxes as it opens, which Windows plays from its sound scheme
through winsound.MessageBeep.

A Linux desktop has the like in its sound theme (dialog-error, dialog-warning, dialog-information),
but the themes ship them as Ogg Vorbis, which NVDA's audio output cannot decode without a decoder
Linda does not have, and a theme player would be a second route to the speakers. So each kind of box
has notes of its own, played through NVDA's tone player as every other tone NVDA makes is.
"""

import winsound
from ctypes import create_string_buffer

import tones
from logHandler import log

#: The notes, as hertz and milliseconds, for each sound winsound.MessageBeep names: an error falls an
#: octave, a warning (Windows' asterisk, which NVDA plays for one) rises, and an exclamation is one
#: note held. MB_OK is the short beep NVDA's browse mode makes for a key it traps.
NOTES: dict[int, tuple[tuple[int, int], ...]] = {
	winsound.MB_ICONHAND: ((880, 90), (440, 160)),
	winsound.MB_ICONASTERISK: ((587, 90), (880, 140)),
	winsound.MB_ICONEXCLAMATION: ((784, 180),),
	winsound.MB_ICONQUESTION: ((659, 90), (784, 140)),
	winsound.MB_OK: ((500, 30),),
}


def _samples(hz: int, length: int) -> bytes:
	from NVDAHelper.localLib import generateBeep

	buf = create_string_buffer(generateBeep(None, hz, length))
	generateBeep(buf, hz, length)
	return buf.raw


def play(soundId: int) -> None:
	"""Play the notes for soundId, one of winsound's MB_ constants, through NVDA's tone player.

	Returns once they are queued, as tones.beep does; a beep made after them cuts them short.
	"""
	notes = NOTES.get(soundId, NOTES[winsound.MB_OK])
	log.io(f"Message beep {soundId:#x}: {notes}")
	# Those who decide on beeps, NVDA Remote among them, hear of it as one beep of the whole length.
	length = sum(ms for _hz, ms in notes)
	if not tones.decide_beep.decide(
		hz=notes[0][0], length=length, left=50, right=50, isSpeechBeepCommand=False
	):
		return
	player = tones.player
	if player is None:
		return
	player.stop()
	player.feed(b"".join(_samples(hz, ms) for hz, ms in notes))
