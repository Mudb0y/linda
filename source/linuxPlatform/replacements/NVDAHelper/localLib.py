"""Linux replacement for NVDAHelper.localLib, skeleton.

Upstream binds nvdaHelperLocal.dll. This has the entry points that code staying on Linux names;
each raises until it has a Linux implementation. Entry points that only Windows-only modules call
(in-process helpers for Office, SysListView32, IAccessible2 and UI Automation) are left out.
"""

from collections.abc import Callable
from ctypes import c_ulonglong, c_void_p
from typing import Any, NoReturn

from linuxPlatform import NoLinuxImplementation


def _notImplemented(name: str) -> Callable[..., NoReturn]:
	def entryPoint(*args: Any, **kwargs: Any) -> NoReturn:
		raise NoLinuxImplementation(f"NVDAHelper.localLib.{name}")

	entryPoint.__name__ = entryPoint.__qualname__ = name
	return entryPoint


VBufRemote_bufferHandle_t = c_void_p
VBufRemote_nodeHandle_t = c_ulonglong

VBuf_createBuffer = _notImplemented("VBuf_createBuffer")
VBuf_destroyBuffer = _notImplemented("VBuf_destroyBuffer")
VBuf_findNodeByAttributes = _notImplemented("VBuf_findNodeByAttributes")
VBuf_getControlFieldNodeWithIdentifier = _notImplemented("VBuf_getControlFieldNodeWithIdentifier")
VBuf_getFieldNodeOffsets = _notImplemented("VBuf_getFieldNodeOffsets")
VBuf_getIdentifierFromControlFieldNode = _notImplemented("VBuf_getIdentifierFromControlFieldNode")
VBuf_getLineOffsets = _notImplemented("VBuf_getLineOffsets")
VBuf_getSelectionOffsets = _notImplemented("VBuf_getSelectionOffsets")
VBuf_getTextInRange = _notImplemented("VBuf_getTextInRange")
VBuf_getTextLength = _notImplemented("VBuf_getTextLength")
VBuf_locateControlFieldNodeAtOffset = _notImplemented("VBuf_locateControlFieldNodeAtOffset")
VBuf_locateTextFieldNodeAtOffset = _notImplemented("VBuf_locateTextFieldNodeAtOffset")
VBuf_setSelectionOffsets = _notImplemented("VBuf_setSelectionOffsets")

calculateCharacterBoundaries = _notImplemented("calculateCharacterBoundaries")
calculateCharacterOffsets = _notImplemented("calculateCharacterOffsets")
calculateWordOffsets = _notImplemented("calculateWordOffsets")

generateBeep = _notImplemented("generateBeep")

displayModel_getCaretRect = _notImplemented("displayModel_getCaretRect")
displayModel_getFocusRect = _notImplemented("displayModel_getFocusRect")
displayModel_getWindowTextInRect = _notImplemented("displayModel_getWindowTextInRect")
displayModel_requestTextChangeNotificationsForWindow = _notImplemented(
	"displayModel_requestTextChangeNotificationsForWindow",
)

audioDucking_shouldDelay = _notImplemented("audioDucking_shouldDelay")
isScreenFullyBlack = _notImplemented("isScreenFullyBlack")
localListeningSocketExists = _notImplemented("localListeningSocketExists")
nvdaInProcUtils_dumpOnCrash = _notImplemented("nvdaInProcUtils_dumpOnCrash")
writeCrashDump = _notImplemented("writeCrashDump")
