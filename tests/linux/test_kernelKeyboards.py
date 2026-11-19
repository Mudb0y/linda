"""The kernel key source with more than one keyboard, the echo of fast typing, and the reason it gives
for losing linda-keyd, with keydStandIn standing in for the helper. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_kernelKeyboards.py
"""

import difflib
import time
from pathlib import Path

from desktopSession import DesktopSession
from keydStandIn import StandIn
from test_kernelKeys import KernelSession, desktop, keyboard, runKernelChild  # noqa: F401

#: The stand-in's own keyboard and a second one, as the kernel would number them.
FIRST, SECOND = 3, 4
#: Typed faster than anyone types, a key every 12 ms or so: the echoes come here sooner after their
#: keys than in a real session, and only at this rate does one come after the next key, as a fast
#: typist's do in a real session.
FAST_TEXT = "the quick brown fox jumps over the lazy dog "
FAST_ROUNDS = 3


def echoesMissed(spoken: list[str], text: str) -> list[tuple[int, str]]:
	"""(position in text, what should have been said) of each echo of a letter or a word typed that is
	not among spoken, in order."""
	expected = []
	for position, c in enumerate(text):
		if c != " ":
			expected.append((position, c))
		elif position and text[position - 1] != " ":
			expected.append((position, text[:position].split()[-1]))
	said = [text for text in spoken if text not in ("", "space")]
	matcher = difflib.SequenceMatcher(None, [wanted for _p, wanted in expected], said, autojunk=False)
	matched = {i for a, _b, size in matcher.get_matching_blocks() for i in range(a, a + size)}
	return [echo for i, echo in enumerate(expected) if i not in matched]


def test_kernelEveryEchoOfFastTypingIsSpoken(desktop: DesktopSession, keyboard: StandIn, tmp_path: Path):  # noqa: F811
	keys = KernelSession(
		desktop,
		keyboard,
		tmp_path,
		"desktop",
		config="\tspeakTypedCharacters = 1\n\tspeakTypedWords = 1\n",
	)
	keyboard.press("End")
	time.sleep(0.3)
	missed = []
	for _round in range(FAST_ROUNDS):
		mark = desktop.mark()
		keyboard.press(*[c if c != " " else "space" for c in FAST_TEXT], hold=0.004, gap=0.004)
		time.sleep(1)
		missed.append(echoesMissed(desktop.spokenSince(mark), FAST_TEXT))
	# Echoes come once the application has the text, often after the next key: each cuts the one before
	# it, as its key does on Windows, rather than wait behind it for a later key to throw it away.
	assert missed == [[]] * FAST_ROUNDS
	keys.quit().assertClean(logNames=("nvda.log",))


SHIFT_CHILD = r"""
source = startSource()
keyboard.down("Shift_L", device=FIRST)
keyboard.down("Shift_L", device=SECOND)
time.sleep(0.2)
keyboard.up("Shift_L", device=FIRST)
time.sleep(0.2)
whileOneHolds = source.keyState(0x10)
keyboard.press("a")
time.sleep(0.3)
keyboard.up("Shift_L", device=SECOND)
time.sleep(0.3)
result = {
	"events": [[e["vk"], e["up"], e["text"]] for e in delivered if not e["repeat"]],
	"whileOneHolds": whileOneHolds,
	"after": source.keyState(0x10),
	"window": windowKeys(),
	"kernel": source.isKernel,
}
source.stop()
keyboard.close()
"""


def test_keyHeldOnTwoKeyboardsIsUpWhenBothLetGo(tmp_path: Path):
	value = runKernelChild(tmp_path, SHIFT_CHILD.replace("FIRST", str(FIRST)).replace("SECOND", str(SECOND)))
	assert value["kernel"]
	# Shift is down once to NVDA and to the application, from the first press to the last release, and
	# a typed while one keyboard still held it is a capital.
	assert value["events"] == [
		[0xA0, False, ""],
		[ord("A"), False, "A"],
		[ord("A"), True, ""],
		[0xA0, True, ""],
	]
	assert value["whileOneHolds"] & 0x8000
	assert not value["after"] & 0x8000
	assert value["window"] == [[42, True], [30, True], [30, False], [42, False]]


VANISH_CHILD = r"""
source = startSource()
keyboard.down("Shift_L", device=FIRST)
keyboard.down("Shift_L", device=SECOND)
time.sleep(0.2)
keyboard.keyboardGone(device=SECOND)
time.sleep(0.2)
keyboard.press("a")
time.sleep(0.3)
stillShifted = source.keyState(0x10)
keyboard.up("Shift_L", device=FIRST)
time.sleep(0.3)
result = {
	"events": [[e["vk"], e["up"], e["text"]] for e in delivered if not e["repeat"]],
	"stillShifted": stillShifted,
	"window": windowKeys(),
}
source.stop()
keyboard.close()
"""


def test_keyboardVanishingLeavesTheKeysAnotherHolds(tmp_path: Path):
	value = runKernelChild(tmp_path, VANISH_CHILD.replace("FIRST", str(FIRST)).replace("SECOND", str(SECOND)))
	assert value["events"] == [
		[0xA0, False, ""],
		[ord("A"), False, "A"],
		[ord("A"), True, ""],
		[0xA0, True, ""],
	]
	assert value["stillShifted"] & 0x8000
	assert value["window"] == [[42, True], [30, True], [30, False], [42, False]]


NVDA_KEY_CHILD = r"""
source = startSource()
keyboard.down("Insert", device=FIRST)
time.sleep(0.2)
keyboard.down("Insert", device=SECOND)
time.sleep(0.2)
keyboard.up("Insert", device=FIRST)
time.sleep(0.2)
keyboard.press("t")
time.sleep(0.2)
keyboard.up("Insert", device=SECOND)
time.sleep(0.3)
result = {
	"events": [[e["vk"], e["up"], e["nvda"]] for e in delivered if not e["repeat"]],
	"verdicts": [[seq, keep] for seq, keep, _latency in keyboard.verdicts],
	"window": windowKeys(),
}
source.stop()
keyboard.close()
"""


def test_nvdaKeyHeldOnTwoKeyboardsStaysNVDAs(tmp_path: Path):
	value = runKernelChild(
		tmp_path, NVDA_KEY_CHILD.replace("FIRST", str(FIRST)).replace("SECOND", str(SECOND))
	)
	# Insert down on the second keyboard is no second press to NVDA, and its release on the first
	# keyboard no release: t, while the second holds it, is still a chord with the NVDA key.
	assert value["events"] == [
		[0x2D, False, True],
		[ord("T"), False, True],
		[ord("T"), True, False],
		[0x2D, True, False],
	]
	# Both presses of Insert and the t of the chord were kept, and the application had none of them.
	assert [keep for _seq, keep in value["verdicts"]] == [True, True, True]
	assert value["window"] == []


LOST_CHILD = r"""
from linuxPlatform.keys import kernel, keydClient

lost = []
sock, welcome = keydClient.connect(path, 5)
source = kernel.KernelKeySource(sock, welcome, onLost=lambda why, emergency: lost.append(why))
source.start(deliver)
keyboard.waitFor(lambda: keyboard.capturing)
gone = threading.Event()
# The source's thread is held up while the helper says goodbye and closes; the heartbeat it sends next
# finds the connection closed, before it has read the goodbye.
source._post(lambda: gone.wait(5))
source._post(lambda: source._send(keydClient.heartbeat(99)))
keyboard.goodbye(8, "presses went unanswered")
# The stand-in's own thread may be in select on the connection, which keeps it open until select ends.
time.sleep(0.2)
gone.set()
deadline = time.monotonic() + 5
while not lost and time.monotonic() < deadline:
	time.sleep(0.02)
result = {"lost": lost}
source.stop()
keyboard.close()
"""


def test_helperLostOnASendGivesTheHelpersReason(tmp_path: Path):
	value = runKernelChild(tmp_path, LOST_CHILD)
	assert value["lost"] == ["it said goodbye: NVDA left keys unanswered (presses went unanswered)"]


#: Every thread of the child, the stand-in's among them, stopped for FROZEN_FOR seconds, as every
#: process is around the machine's sleep while the monotonic clock runs on. The stand-in says it lets
#: go after two seconds of silence and keeps to thirty, as the helper takes its own freeze into account.
FROZEN_FOR = 5
FROZEN_STAND_IN = {"heartbeatTimeout": 30.0, "announcedHeartbeatTimeout": 2.0}
FREEZE_CHILD = r"""
import signal, subprocess


def freeze():
	subprocess.Popen(["sh", "-c", f"sleep {FROZEN_FOR}; kill -CONT {os.getpid()}"])
	os.kill(os.getpid(), signal.SIGSTOP)
	return time.monotonic()


source = startSource()
kernelBefore = source.isKernel
said.clear()
freeze()
time.sleep(2)
afterFreeze = {"kernel": source.isKernel, "said": list(said)}
delivered.clear()
windowKeys()
keyboard.press("Insert+t")
time.sleep(0.3)
chord = {"nvda": [[e["vk"], e["nvda"]] for e in presses()], "window": [key for key in windowKeys() if key[1]]}
# Now the helper falls silent, and stays so after another freeze: it is given up within its bound and
# the slack, counted from the wake.
keyboard._sendClient = lambda message: None
thawed = freeze()
while source.isKernel and time.monotonic() < thawed + 10:
	time.sleep(0.02)
result = {
	"kernelBefore": kernelBefore,
	"afterFreeze": afterFreeze,
	"chord": chord,
	"givenUpAfter": time.monotonic() - thawed,
}
source.stop()
keyboard.close()
"""


def test_freezeOfEveryThreadIsNoSilenceOfTheHelpers(tmp_path: Path):
	value = runKernelChild(
		tmp_path, f"FROZEN_FOR = {FROZEN_FOR}\n" + FREEZE_CHILD, standIn=FROZEN_STAND_IN, timeout=120
	)
	assert value["kernelBefore"]
	assert value["afterFreeze"] == {"kernel": True, "said": []}
	assert value["chord"] == {"nvda": [[0x2D, True], [ord("T"), True]], "window": []}
	# The helper's two seconds and the second of slack, with the half second the beat takes to look.
	assert 2.5 <= value["givenUpAfter"] <= 4.0, value["givenUpAfter"]
