"""App module for marco, MATE's window manager.

While Alt+Tab is held, and as the workspace changes, marco shows a popup that never takes the focus,
naming the window that releasing Alt would activate or the workspace switched to. The popup is a
window of marco's own on the accessibility bus, and the name is that of a label inside it whose role
is status bar, which marco renames for each choice. Speaking each new name gives what NVDA says for
the Windows task switcher, and what Orca says for this one.

When consecutive switcher choices share the exact same window title, GTK suppresses the duplicate
accessible-name notification on the status bar. Watching navigation keys while the switcher popup
remains showing repeats the unchanged title after a brief settle window, without double-speaking
choices whose titles differ (issue #59).
"""

import time

import appModuleHandler
import controlTypes
import core
import inputCore
import queueHandler
import speech
import ui
from linuxPlatform import inputHook
from linuxPlatform.atspi import handler as atspiHandler
from linuxPlatform.atspi.client import Node, NodeGone

#: Virtual-key codes for Alt (generic, left, right).
_ALT_VKS = frozenset({0x12, 0xA4, 0xA5})
#: Navigation keys that advance or move selection inside marco's switcher popup (Tab and arrows).
_SWITCH_NAV_VKS = frozenset({0x09, 0x25, 0x26, 0x27, 0x28})
#: PC set 1 scan code of the key above Tab (Alt+Above_Tab switches windows of the same application).
_ABOVE_TAB_SCAN = 0x29
#: Milliseconds to wait for accessible-name before repeating an unchanged switcher title.
_REPEAT_DELAY_MS = 80


def _isAltHeld() -> bool:
	try:
		return any(inputHook.getKeyState(vk) & 0x8000 for vk in _ALT_VKS)
	except Exception:  # noqa: BLE001
		return False


class AppModule(appModuleHandler.AppModule):
	#: Events heard from marco although it never has the focus or the foreground.
	backgroundEvents = frozenset({"nameChange"})

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._switcherNode: Node | None = None
		self._lastSpokenName = ""
		self._lastNameChangeAt = 0.0
		self._altReleasedAt = 0.0
		self._pendingTimer = None
		inputCore.decide_handleRawKey.register(self._onRawKey)

	def terminate(self):
		inputCore.decide_handleRawKey.unregister(self._onRawKey)
		self._cancelPending()
		self._switcherNode = None
		super().terminate()

	def _cancelPending(self) -> None:
		timer, self._pendingTimer = self._pendingTimer, None
		if timer is not None:
			timer.Stop()

	def event_nameChange(self, obj, nextHandler):
		if obj.role == controlTypes.Role.STATUSBAR and obj.name:
			self._cancelPending()
			self._switcherNode = obj.node
			self._lastSpokenName = obj.name
			self._lastNameChangeAt = time.monotonic()
			# A newer choice makes the one still being spoken worthless.
			speech.cancelSpeech()
			ui.message(obj.name)
			return
		nextHandler()

	def _onRawKey(self, pressed: bool) -> bool:
		if self._switcherNode is None:
			return True
		event = inputHook.deliveringKey()
		if event is None:
			return True
		if not pressed:
			if event.vkCode in _ALT_VKS:
				self._altReleasedAt = time.monotonic()
			return True
		if (event.vkCode in _SWITCH_NAV_VKS or event.scanCode == _ABOVE_TAB_SCAN) and _isAltHeld():
			pressedAt = time.monotonic()
			queueHandler.queueFunction(queueHandler.eventQueue, self._scheduleRepeat, pressedAt)
		return True

	def _scheduleRepeat(self, pressedAt: float) -> None:
		if self._lastNameChangeAt >= pressedAt or self._altReleasedAt >= pressedAt:
			return
		self._cancelPending()
		self._pendingTimer = core.callLater(_REPEAT_DELAY_MS, self._speakIfSameTitle, pressedAt)

	def _speakIfSameTitle(self, pressedAt: float) -> None:
		self._pendingTimer = None
		if self._lastNameChangeAt >= pressedAt or self._altReleasedAt >= pressedAt or not _isAltHeld():
			return
		node = self._switcherNode
		if node is None:
			return
		try:
			c = atspiHandler.client()
			states = c.states(node)
			name = c.name(node)
		except NodeGone:
			self._switcherNode = None
			return
		if "showing" not in states or not name:
			self._switcherNode = None
			return
		if name != self._lastSpokenName:
			return
		speech.cancelSpeech()
		ui.message(name)
