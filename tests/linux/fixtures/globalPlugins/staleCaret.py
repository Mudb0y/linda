"""A global plugin for test_webCommentBoxRace.py: with LINDA_STALE_CARET set to an editor's name, the
editor's own caret offset is given one read late each time it changes, as Firefox gives it on a busy
page such as YouTube while the caret moves from one paragraph to another: the paragraph the caret
left already says it has none, while the editor still points at it. Unset, the plugin does nothing.
"""

import os

import globalPluginHandler
from linuxPlatform.atspi import client as clientModule
from linuxPlatform.atspi.client import NodeGone

ENVIRONMENT_VARIABLE = "LINDA_STALE_CARET"


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	def __init__(self) -> None:
		super().__init__()
		self._original = None
		name = os.environ.get(ENVIRONMENT_VARIABLE)
		if not name:
			return
		original = self._original = clientModule.Client.caretOffset
		#: The offset last given for each editor, by its node's key.
		given: dict[tuple[str, str], int] = {}

		def caretOffset(client, node):
			offset = original(client, node)
			try:
				if client.name(node) != name:
					return offset
			except NodeGone:
				return offset
			before = given.get(node.key)
			given[node.key] = offset
			# The first read after a change still gives the offset from before it.
			return before if before is not None and before != offset else offset

		clientModule.Client.caretOffset = caretOffset

	def terminate(self) -> None:
		if self._original is not None:
			clientModule.Client.caretOffset = self._original
		super().terminate()
