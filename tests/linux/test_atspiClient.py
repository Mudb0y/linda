"""AT-SPI event filtering and lazy any_data decoding in Client.listen (issue 70).

Run under the rig:

	nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_atspiClient.py
"""

import sys
import types

import harness
import pytest

if str(harness.SOURCE) not in sys.path:
	sys.path.insert(0, str(harness.SOURCE))

from linuxPlatform.atspi import client as atspiClient


class _FakeAccessible:
	def __init__(self, busName: str, path: str) -> None:
		self.app = types.SimpleNamespace(bus_name=busName)
		self.path = path


class _FakeEvent:
	def __init__(
		self,
		eventType: str,
		source: object,
		detail1: int = 0,
		detail2: int = 0,
		*,
		anyData: object = None,
		forbidAnyData: bool = False,
		forbidSource: bool = False,
		anyDataError: Exception | None = None,
	) -> None:
		self.type = eventType
		self._source = source
		self.detail1 = detail1
		self.detail2 = detail2
		self._anyData = anyData
		self._forbidAnyData = forbidAnyData
		self._forbidSource = forbidSource
		self._anyDataError = anyDataError
		self.anyDataRead = False
		self.sourceRead = False

	@property
	def source(self):
		self.sourceRead = True
		if self._forbidSource:
			raise AssertionError(f"source should not be read for {self.type!r}")
		return self._source

	@property
	def any_data(self):
		self.anyDataRead = True
		if self._forbidAnyData:
			raise AssertionError(f"any_data should not be read for {self.type!r}")
		if self._anyDataError is not None:
			raise self._anyDataError
		return self._anyData


def _captureListener(eventTypes: list[str], monkeypatch: pytest.MonkeyPatch):
	captured = {}

	class _FakeListener:
		def __init__(self) -> None:
			self.registered: list[str] = []

		def register(self, eventType: str) -> None:
			self.registered.append(eventType)

	def _newListener(callback):
		captured["onEvent"] = callback
		listener = _FakeListener()
		captured["listener"] = listener
		return listener

	monkeypatch.setattr(atspiClient, "_hasNoBus", False)
	monkeypatch.setattr(atspiClient.Atspi.EventListener, "new", staticmethod(_newListener))
	client = object.__new__(atspiClient.Client)
	client._listener = None
	received: list[tuple] = []
	client.listen(eventTypes, lambda *args: received.append(args))
	return captured["onEvent"], received, captured["listener"]


@pytest.mark.parametrize(
	("eventType", "registered", "expected"),
	[
		("object:state-changed:focused", ("object:state-changed",), True),
		("object:state-changed", ("object:state-changed",), True),
		("object:state-changed-extra", ("object:state-changed",), False),
		("object:children-changed:add", ("object:state-changed", "object:text-changed"), False),
		("object:bounds-changed", ("object:state-changed", "window:activate"), False),
		("focus:", ("focus:",), True),
		("focus:detail", ("focus:",), True),
		("window:activate", ("window:activate",), True),
	],
)
def test_matchesEventTypes(eventType: str, registered: tuple[str, ...], expected: bool) -> None:
	assert atspiClient._matchesEventTypes(eventType, registered) is expected


def test_unregisteredEventsDoNotReadAnyDataOrSource(monkeypatch: pytest.MonkeyPatch) -> None:
	onEvent, received, _listener = _captureListener(["object:state-changed", "window:activate"], monkeypatch)
	source = _FakeAccessible(":1.42", "/org/a11y/atspi/accessible/1")
	for eventType in (
		"object:children-changed:add",
		"object:children-changed:remove",
		"object:bounds-changed",
		"document:load-complete",
		"",
	):
		event = _FakeEvent(eventType, source, forbidAnyData=True, forbidSource=True)
		onEvent(event)
		assert not event.anyDataRead
		assert not event.sourceRead
	assert received == []


def test_registeredEventsWithoutAnyDataNeverTouchAnyData(monkeypatch: pytest.MonkeyPatch) -> None:
	onEvent, received, _listener = _captureListener(
		[
			"object:state-changed",
			"object:text-caret-moved",
			"window:activate",
			"focus:",
		],
		monkeypatch,
	)
	source = _FakeAccessible(":1.42", "/org/a11y/atspi/accessible/2")
	for eventType in (
		"object:state-changed:focused",
		"object:state-changed:defunct",
		"object:text-caret-moved",
		"window:activate",
		"focus:",
	):
		event = _FakeEvent(eventType, source, detail1=1, detail2=2, forbidAnyData=True)
		onEvent(event)
		assert not event.anyDataRead
	assert len(received) == 5
	assert all(item[4] is None for item in received)


def test_registeredEventsWithAnyDataDecodeStringsNodesAndInvalidGValues(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	onEvent, received, _listener = _captureListener(
		[
			"object:active-descendant-changed",
			"object:property-change:accessible-name",
			"object:announcement",
			"object:text-changed",
		],
		monkeypatch,
	)
	source = _FakeAccessible(":1.42", "/org/a11y/atspi/accessible/10")
	child = _FakeAccessible(":1.42", "/org/a11y/atspi/accessible/11")

	monkeypatch.setattr(atspiClient.Atspi, "Accessible", _FakeAccessible)
	onEvent(_FakeEvent("object:active-descendant-changed", source, anyData=child))
	onEvent(_FakeEvent("object:property-change:accessible-name", source, anyData="OK"))
	onEvent(_FakeEvent("object:announcement", source, anyData="Build finished"))
	onEvent(_FakeEvent("object:text-changed:insert:system", source, detail1=0, detail2=1, anyData="x"))
	onEvent(
		_FakeEvent(
			"object:active-descendant-changed",
			source,
			anyDataError=TypeError("Invalid type"),
		),
	)

	assert len(received) == 5
	assert isinstance(received[0][4], atspiClient.Node)
	assert received[0][4].key == (":1.42", "/org/a11y/atspi/accessible/11")
	assert received[1][4] == "OK"
	assert received[2][4] == "Build finished"
	assert received[3][4] == "x"
	assert received[4][4] is None
