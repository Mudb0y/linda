"""AutoPropertyObject's caches emptied on the main thread while another thread makes instances, as the key
sources' threads make a gesture for every key. Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_propertyCaches.py
"""

from pathlib import Path

import harness

#: Every core pump empties the caches; a thread makes instances all the while, its turn coming every
#: microsecond so that it comes in the middle of the main thread's look at them.
RACE_CHILD = r"""
import sys, threading, baseObject

sys.setswitchinterval(1e-6)


class Thing(baseObject.AutoPropertyObject):
	pass


stop = threading.Event()


def make():
	kept = []
	while not stop.is_set():
		kept.append(Thing())
		if len(kept) > 500:
			kept.clear()


maker = threading.Thread(target=make)
maker.start()
errors = 0
for _turn in range(3000):
	try:
		baseObject.AutoPropertyObject.invalidateCaches()
	except RuntimeError:
		errors += 1
stop.set()
maker.join()
result = {"errors": errors}
"""


def test_cachesEmptiedWhileAnotherThreadMakesInstances(tmp_path: Path):
	done = harness.runChild(tmp_path, RACE_CHILD, timeout=120)
	assert done.error is None, f"{done.error}\n{done.stderr[-4000:]}"
	assert done.value == {"errors": 0}
