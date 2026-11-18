"""A global plugin for test_mouse.py: with LINDA_MOUSE_TIMING set, it logs what NVDA's main thread
spends on each mouse move it follows, as "mouse move: TOTAL ms, hit test HIT ms at X, Y". TOTAL is
all of mouseHandler.executeMouseMoveEvent; HIT the part of it that finds the object under the point.
Unset, the plugin does nothing.
"""

import os
import time

import globalPluginHandler
import mouseHandler
from linuxPlatform.atspi import handler
from logHandler import log

ENVIRONMENT_VARIABLE = "LINDA_MOUSE_TIMING"


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._move = self._hit = None
		if not os.environ.get(ENVIRONMENT_VARIABLE):
			return
		self._move, self._hit = mouseHandler.executeMouseMoveEvent, handler.nodeForRelation
		hitTimes: list[float] = []

		def nodeForRelation(relation):
			started = time.perf_counter()
			try:
				return self._hit(relation)
			finally:
				if isinstance(relation, tuple):
					hitTimes.append(time.perf_counter() - started)

		def executeMouseMoveEvent(x, y):
			hitTimes.clear()
			started = time.perf_counter()
			try:
				return self._move(x, y)
			finally:
				total = (time.perf_counter() - started) * 1000
				log.info(f"mouse move: {total:.2f} ms, hit test {sum(hitTimes) * 1000:.2f} ms at {x}, {y}")

		handler.nodeForRelation = nodeForRelation
		mouseHandler.executeMouseMoveEvent = executeMouseMoveEvent

	def terminate(self) -> None:
		if self._move is not None:
			mouseHandler.executeMouseMoveEvent = self._move
			handler.nodeForRelation = self._hit
