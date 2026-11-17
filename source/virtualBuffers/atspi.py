"""Browse mode for web documents read over AT-SPI: Firefox now, Chromium later.

NVDA's own buffer storage holds the document (linuxPlatform.vbufLib), and the engine's renderer fills
it (linuxPlatform.vbufEngine), walking the document over its own connection on a thread. Nothing is
injected into the browser, so there is no binding handle: prepare does not ask for one.
"""

import ctypes
import threading
import time

import aria
import queueHandler
import textInfos
from controlTypes import Role, State
from linuxPlatform import vbufEngine, vbufLib
from linuxPlatform.atspi import accessibleFor, handler
from linuxPlatform.atspi.client import Node, NodeGone
from logHandler import log

from . import VBufStorage_findMatch_word, VirtualBuffer, VirtualBufferTextInfo

#: The object path every accessible of the ATK bridge and of Qt has, with a number at its end.
_PATH_PREFIX = "/org/a11y/atspi/accessible/"


class Identifiers:
	"""docHandle and ID for AT-SPI identities: a small number per bus name, and the number at the end
	of an object path cast to signed 32 bits. The ATK bridge counts up from 1 and never reuses a
	number, and Qt counts from 2^31; the engine's renderer applies the same rule."""

	def __init__(self) -> None:
		self._docHandles: dict[str, int] = {}
		self._busNames: dict[int, str] = {}
		self._lock = threading.Lock()

	def docHandle(self, busName: str) -> int:
		with self._lock:
			handle = self._docHandles.get(busName)
			if handle is None:
				handle = self._docHandles[busName] = len(self._docHandles) + 1
				self._busNames[handle] = busName
			return handle

	def busName(self, docHandle: int) -> str | None:
		return self._busNames.get(docHandle)

	@staticmethod
	def id(path: str) -> int:
		"""The ID for path; 0, which no accessible has, for a path without a number at its end."""
		tail = path.rpartition("/")[2]
		if not tail.isdecimal() or int(tail) >= 2**32:
			return 0
		return ctypes.c_int32(int(tail)).value

	@staticmethod
	def path(ID: int) -> str:
		return f"{_PATH_PREFIX}{ID & 0xFFFFFFFF}"

	def of(self, node: Node) -> tuple[int, int]:
		return self.docHandle(node.busName), self.id(node.path)


#: One numbering of bus names for every document, so that an identifier in one buffer means the same in
#: another.
identifiers = Identifiers()

_FORM_FIELD_ROLES = (
	Role.BUTTON,
	Role.CHECKBOX,
	Role.COMBOBOX,
	Role.LIST,
	Role.TREEVIEW,
	Role.RADIOBUTTON,
	Role.TAB,
	Role.TOGGLEBUTTON,
	Role.MENUBUTTON,
	Role.SLIDER,
	Role.SPINBUTTON,
)


def _role(*roles: Role) -> list[str]:
	return [str(r.value) for r in roles]


def _state(state: State) -> str:
	return f"state_{state.value}"


class AtSpiBufferTextInfo(VirtualBufferTextInfo):
	def _normalizeControlField(self, attrs: textInfos.ControlField):
		role = Role(int(attrs.get("role", Role.UNKNOWN.value)))
		states = set()
		for key in list(attrs):
			if key.startswith("state_"):
				states.add(State(int(key[6:])))
		xmlRoles = attrs.get("xml-roles", "").split(" ")
		landmark = next((r for r in xmlRoles if r in aria.landmarkRoles), None)
		if role == Role.LANDMARK and landmark is None:
			landmark = "region"
		if role in (Role.LANDMARK, Role.SECTION) and xmlRoles[0] == "region":
			role = Role.REGION
		if landmark:
			attrs["landmark"] = landmark
		attrs["role"] = role
		attrs["states"] = states
		return super()._normalizeControlField(attrs)

	def _get_location(self):
		obj = self._getNVDAObjectFromOffset(self._startOffset)
		return obj.location if obj else None


class AtSpiBuffer(VirtualBuffer):
	TextInfo = AtSpiBufferTextInfo

	def __init__(self, rootNVDAObject):
		root = rootNVDAObject.node
		self.busName: str = root.busName
		self.docPath: str = root.path
		self.complete = False
		self._docURL: str | None = None
		super().__init__(rootNVDAObject, backendName="atspi")

	def prepare(self):
		# No helper is injected into the browser, so there is no binding handle to wait for.
		self.shouldPrepare = False
		self.loadBuffer()

	def loadBuffer(self):
		self.isLoading = True
		import wx

		self._loadProgressCallLater = wx.CallLater(1000, self._loadProgress)
		threading.Thread(
			name=f"{self.__class__.__module__}.{self.loadBuffer.__qualname__}",
			target=self._loadWithEngine,
			args=(self.busName, self.docPath, identifiers.docHandle(self.busName)),
			daemon=True,
		).start()

	def _loadWithEngine(self, busName: str, path: str, docHandle: int) -> None:
		start = time.perf_counter()
		handle = None
		try:
			rendered = vbufEngine.render(busName, path, docHandle)
			renderedAt = time.perf_counter()
			try:
				handle = vbufLib.create()
				vbufLib.build(handle, rendered.stream)
			finally:
				rendered.stream.free()
		except Exception:
			if handle:
				vbufLib.destroy(handle)
			log.exception("The engine could not render the document")
			queueHandler.queueFunction(queueHandler.eventQueue, self._loadBufferDone, success=False)
			return
		self.VBufHandle = ctypes.c_void_p(handle)
		self._docURL = rendered.info.get("docURL") or None
		self.complete = bool(rendered.info.get("complete"))
		log.info(
			f"Buffer rendered by the engine in {renderedAt - start:.3f} s, built in "
			f"{time.perf_counter() - renderedAt:.4f} s, {vbufLib.lib().VBuf_getTextLength(handle)} "
			f"code units: {rendered.info}",
		)
		queueHandler.queueFunction(queueHandler.eventQueue, self._loadBufferDone)

	def __contains__(self, obj):
		node = getattr(obj, "node", None)
		if node is None or node.busName != self.busName:
			return False
		if node == self.rootNVDAObject.node:
			return True
		if not self.VBufHandle:
			return False
		docHandle, ID = identifiers.of(node)
		found = ctypes.c_uint64()
		vbufLib.lib().VBuf_getControlFieldNodeWithIdentifier(
			self.VBufHandle, docHandle, ID, ctypes.byref(found)
		)
		if not found:
			return False
		return not self._isNVDAObjectInApplication(obj)

	def _get_isAlive(self):
		# Never a call into the browser: a hung browser must not stall every focus change (#7818).
		if self.isLoading:
			return True
		return not handler.client().isHung(self.busName)

	def getNVDAObjectFromIdentifier(self, docHandle, ID):
		busName = identifiers.busName(docHandle)
		if not busName or not ID:
			return None
		node = accessibleFor.nodeFor(busName, identifiers.path(ID))
		if node is None:
			return None
		return handler.getObject(node)

	def getIdentifierFromNVDAObject(self, obj):
		return identifiers.of(obj.node)

	def _get_documentConstantIdentifier(self):
		return self._docURL

	def _shouldIgnoreFocus(self, obj):
		if obj.role == Role.DOCUMENT and State.EDITABLE not in obj.states:
			return True
		return super()._shouldIgnoreFocus(obj)

	def _searchableAttribsForNodeType(self, nodeType):
		if nodeType.startswith("heading") and nodeType[7:].isdigit():
			return {"role": _role(Role.HEADING), "level": [nodeType[7:]]}
		simple = {
			"heading": _role(Role.HEADING),
			"list": _role(Role.LIST),
			"listItem": _role(Role.LISTITEM),
			"graphic": _role(Role.GRAPHIC),
			"separator": _role(Role.SEPARATOR),
			"radioButton": _role(Role.RADIOBUTTON),
			"comboBox": _role(Role.COMBOBOX),
			"checkBox": _role(Role.CHECKBOX, Role.SWITCH),
			"button": _role(Role.BUTTON, Role.TOGGLEBUTTON, Role.MENUBUTTON),
			"frame": _role(Role.INTERNALFRAME),
			"blockQuote": _role(Role.BLOCKQUOTE),
			"tab": _role(Role.TAB),
			"progressBar": _role(Role.PROGRESSBAR),
			"toggleButton": _role(Role.TOGGLEBUTTON),
			"menuItem": _role(Role.MENUITEM, Role.CHECKMENUITEM, Role.RADIOMENUITEM),
			"math": _role(Role.MATH),
			"slider": _role(Role.SLIDER),
		}
		if nodeType in simple:
			return {"role": simple[nodeType]}
		if nodeType == "link":
			return {"role": _role(Role.LINK)}
		if nodeType == "visitedLink":
			return {"role": _role(Role.LINK), _state(State.VISITED): ["1"]}
		if nodeType == "unvisitedLink":
			return {"role": _role(Role.LINK), _state(State.VISITED): [None]}
		if nodeType == "table":
			return {"role": _role(Role.TABLE)}
		if nodeType == "formField":
			return [
				{"role": _role(*_FORM_FIELD_ROLES), _state(State.READONLY): [None]},
				{"role": _role(Role.EDITABLETEXT), _state(State.EDITABLE): ["1"]},
			]
		if nodeType == "edit":
			return {"role": _role(Role.EDITABLETEXT), _state(State.EDITABLE): ["1"]}
		if nodeType == "focusable":
			return {_state(State.FOCUSABLE): ["1"]}
		if nodeType == "landmark":
			return [
				{"role": _role(Role.LANDMARK)},
				{"xml-roles": [VBufStorage_findMatch_word(r) for r in aria.landmarkRoles]},
			]
		if nodeType == "article":
			return {"role": _role(Role.ARTICLE)}
		if nodeType == "grouping":
			return {"role": _role(Role.GROUPING)}
		if nodeType == "figure":
			return {"role": _role(Role.FIGURE)}
		return None

	def event_stateChange(self, obj, nextHandler):
		if not self.isAlive:
			import treeInterceptorHandler

			return treeInterceptorHandler.killTreeInterceptor(self)
		return nextHandler()


def treeInterceptorClassFor(obj) -> type | None:
	"""The buffer class for a document object, or None: Gecko's web documents that are not editable,
	as mozilla.py chooses on Windows, while both the storage and the renderer are there."""
	if not (vbufLib.isAvailable() and vbufEngine.isAvailable()):
		return None
	try:
		if obj.atspiRole == "document web" and "editable" not in obj.atspiStates:
			return AtSpiBuffer
	except NodeGone:
		pass
	return None
