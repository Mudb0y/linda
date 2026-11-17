"""The engine's document renderer against Firefox, without NVDA: a page renders whole and in order,
the first action after it is not dropped, each change to a page comes as an update that leaves the
buffer as a fresh render would, following a link ends the old document, and a large page comes top
first without holding Firefox up.

BASEDREADER_VBUF_LIB=ROOT/engine/target/vbuf/libnvdaVbuf.so \
BASEDREADER_VBUF_ENGINE=ROOT/engine/target/release/libbre_vbuf.so \
BASEDREADER_RIG_WM=marco nix develop . -c tests/linux/rig python3 -m pytest -q tests/linux/test_vbufEngine.py
"""

import array
import ctypes
import importlib.util
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import desktopSession
import harness
import pytest
import webSession
from desktopSession import DesktopSession

ENGINE_CRATE = harness.ROOT / "engine" / "crates" / "vbufengine"
MAKE_PAGE = harness.HERE / "bench" / "makePage.py"
DOC_HANDLE = 1
#: Seconds the whole of basic.html may take to render, from the call to its return.
BASIC_RENDER_BOUND = 0.5
#: Seconds from a change on the page to the batch that carries it.
CHANGE_BOUND = 0.3
#: Seconds without a batch after which a change counts as fully delivered.
QUIET = 0.5
#: Seconds for a followed link to load and the old document to be reported gone.
FOLLOW_BOUND = 10.0
#: Seconds for the top of the large page, for all of it, and for any other client's call meanwhile.
FIRST_PART_BOUND = 1.0
LARGE_BOUND = 15.0
CALL_BOUND = 0.3
LARGE_TITLE = "Basedreader bench large page"
#: Seconds a page has to finish loading, and how long it must stay loaded to count as finished.
LOAD_BOUND = 120.0
STEADY = 0.5
#: Text basic.html reads as, in this order.
BASIC_TEXT = (
	"Basic page",
	"The first paragraph has a link to the contact section on this page.",
	"The second page",
	"An emoji 😀 sits in this sentence.",
	"Zażółć gęślą jaźń.",
	"A quoted sentence.",
	"First item",
	"Layout cell four",
	"Prices",
	"Fruit",
	"Price",
	"Apple",
	"Pear",
	"Your name",
	"Subscribe",
	"Small",
	"Large",
	"Red",
	"Press me",
	"A small square has alt text.",
	"second is a link with only an unlabelled image.",
	"Text inside the frame.",
	"Embedded application",
	"Complementary text.",
	"Write to us.",
)
#: The buttons of dynamic.html whose change is in the document, in an order where each still has
#: something to change. Changing a link's address and label sends no signal at all, and the others
#: move focus or the address only.
DYNAMIC_CHANGES = (
	"Add paragraph",
	"Insert section",
	"Remove paragraph",
	"Replace text",
	"Append text",
	"Expand region",
	"Collapse region",
	"Hide section",
	"Update live text",
	"Set field value",
	"Clear list",
	"Add 200 items",
	"Change table row",
	"Switch view",
)


def _loadSource(name: str) -> ModuleType:
	"""source/linuxPlatform/<name>.py on its own, without NVDA or source/ on sys.path."""
	spec = importlib.util.spec_from_file_location(name, harness.SOURCE / "linuxPlatform" / f"{name}.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


@pytest.fixture(scope="module")
def libraries() -> tuple[ModuleType, ModuleType]:
	webSession.requireBrowseLibraries()
	return _loadSource("vbufEngine"), _loadSource("vbufLib")


@pytest.fixture
def engine(libraries) -> Iterator[ModuleType]:
	vbufEngine, _ = libraries
	yield vbufEngine
	vbufEngine.shutdown()


@pytest.fixture
def vbufLib(libraries) -> ModuleType:
	return libraries[1]


@pytest.fixture
def desktop(tmp_path: Path) -> Iterator[DesktopSession]:
	with DesktopSession(tmp_path / "desktop") as session:
		yield session


class Units:
	"""A render stream of the test's own, in the shape vbufLib takes from the engine."""

	def __init__(self, units: list[int]) -> None:
		self._array = (ctypes.c_uint16 * max(len(units), 1))(*units)
		self.pointer = ctypes.addressof(self._array)
		self.length = len(units)


def _units(stream) -> list[int]:
	return list(memoryview(ctypes.string_at(stream.pointer, stream.length * 2)).cast("H"))


class Field:
	"""A control field of a render stream: its identifier, flags, attributes and content, in which
	each item is a Field, a text as (str, attributes), or ("R", identifier) for a kept field."""

	def __init__(self, identifier: tuple[int, int], flags: int, attributes: dict[str, str]) -> None:
		self.identifier = identifier
		self.flags = flags
		self.attributes = attributes
		self.content: list = []

	def walk(self) -> Iterator["Field"]:
		yield self
		for item in self.content:
			if isinstance(item, Field):
				yield from item.walk()


class _Reader:
	def __init__(self, units: list[int]) -> None:
		self.units = units
		self.at = 0

	def more(self) -> bool:
		return self.at < len(self.units)

	def op(self) -> str:
		self.at += 1
		return chr(self.units[self.at - 1])

	def integer(self, end: str) -> int:
		start = self.at
		while chr(self.units[self.at]) != end:
			self.at += 1
		value = int(bytes(self.units[start : self.at]).decode())
		self.at += 1
		return value

	def string(self) -> str:
		length = self.integer(":")
		data = self.units[self.at : self.at + length]
		self.at += length
		return _decode(data)

	def attributes(self) -> dict[str, str]:
		count = self.integer(";")
		return {self.string(): self.string() for _ in range(count)}

	def field(self) -> Field:
		docHandle = self.integer(",")
		ID = self.integer(",")
		flags = self.integer(",")
		node = Field((docHandle, ID), flags, self.attributes())
		while (op := self.op()) != "E":
			if op == "C":
				node.content.append(self.field())
			elif op == "T":
				text = self.string()
				node.content.append((text, self.attributes()))
			elif op == "R":
				node.content.append(("R", (self.integer(","), self.integer(";"))))
			else:
				raise AssertionError(f"unexpected {op!r} at {self.at - 1}")
		return node


def _decode(units: list[int]) -> str:
	return array.array("H", units).tobytes().decode("utf-16-le", "surrogatepass")


def parseDocument(units: list[int]) -> Field:
	reader = _Reader(units)
	assert reader.op() == "C"
	root = reader.field()
	assert not reader.more(), "more after the document's field"
	return root


def parseBatch(units: list[int]) -> list[tuple[tuple[int, int], Field]]:
	"""The S operations of an update: each identifier replaced, and its new field."""
	reader = _Reader(units)
	replaced = []
	while reader.more():
		assert reader.op() == "S"
		identifier = (reader.integer(","), reader.integer(";"))
		assert reader.op() == "C"
		replaced.append((identifier, reader.field()))
	return replaced


def _encodeString(text: str, out: list[int]) -> None:
	data = text.encode("utf-16-le", "surrogatepass")
	out.extend(map(ord, str(len(data) // 2)))
	out.append(ord(":"))
	out.extend(memoryview(data).cast("H"))


def _encodeAttributes(attributes: dict[str, str], out: list[int]) -> None:
	out.extend(map(ord, f"{len(attributes)};"))
	for name, value in attributes.items():
		_encodeString(name, out)
		_encodeString(value, out)


def encode(node: Field, out: list[int]) -> list[int]:
	out.extend(map(ord, f"C{node.identifier[0]},{node.identifier[1]},{node.flags},"))
	_encodeAttributes(node.attributes, out)
	for item in node.content:
		if isinstance(item, Field):
			encode(item, out)
		else:
			out.append(ord("T"))
			_encodeString(item[0], out)
			_encodeAttributes(item[1], out)
	out.append(ord("E"))
	return out


def _keptIn(node: Field) -> int:
	"""How many R operations are in node's field."""
	return sum(
		_keptIn(item) if isinstance(item, Field) else int(item[0] == "R" and isinstance(item[1], tuple))
		for item in node.content
	)


def applyBatch(document: Field, batch: list[tuple[tuple[int, int], Field]]) -> Field:
	"""document with each field the batch names replaced, its R operations by the fields kept: what
	the buffer holds once it has applied the batch."""
	byIdentifier = {field.identifier: field for field in document.walk()}

	def resolve(node: Field) -> Field:
		content = []
		for item in node.content:
			if isinstance(item, Field):
				content.append(resolve(item))
			elif item[0] == "R":
				assert item[1] in byIdentifier, f"R names {item[1]}, which the buffer does not hold"
				content.append(byIdentifier[item[1]])
			else:
				content.append(item)
		node.content = content
		return node

	replacements = {identifier: resolve(field) for identifier, field in batch}
	for identifier in replacements:
		assert identifier in byIdentifier, f"S names {identifier}, which the buffer does not hold"

	def replace(node: Field) -> Field:
		if node.identifier in replacements:
			node = replacements[node.identifier]
		node.content = [replace(i) if isinstance(i, Field) else i for i in node.content]
		return node

	return replace(document)


def bufferText(vbufLib: ModuleType, stream) -> str:
	"""The plain text of a buffer built from stream through libnvdaVbuf."""
	handle = vbufLib.create()
	try:
		vbufLib.build(handle, stream)
		length = vbufLib.lib().VBuf_getTextLength(handle)
		return vbufLib.VBuf_getTextInRange(handle, 0, length, False) or ""
	finally:
		vbufLib.destroy(handle)


def documentFor(title: str):
	"""The document web named title, once it has stopped loading, with its application's bus name
	and its object path."""
	deadline = time.monotonic() + LOAD_BOUND
	while True:
		document, seen = webSession._findDocument(title, ready=True)
		if document is not None:
			# A page can be marked loaded and then loading again while its parts come in.
			time.sleep(STEADY)
			again, seen = webSession._findDocument(title, ready=True)
			if again is not None and again.path == document.path:
				return document, document.app.bus_name, document.path
		assert time.monotonic() < deadline, f"no document {title!r} done loading; documents: {seen}"
		time.sleep(0.1)


def _matches(root, role):
	Atspi = desktopSession._atspi()
	rule = Atspi.MatchRule.new(
		None,
		Atspi.CollectionMatchType.ALL,
		None,
		Atspi.CollectionMatchType.ALL,
		[role],
		Atspi.CollectionMatchType.ANY,
		None,
		Atspi.CollectionMatchType.ALL,
		False,
	)
	return Atspi.Collection.get_matches(root, rule, Atspi.CollectionSortOrder.CANONICAL, 0, True) or []


def named(root, role, name: str):
	for found in _matches(root, role):
		if found.get_name() == name:
			return found
	raise AssertionError(f"no {role} named {name!r}")


def documentURL(document) -> str:
	Atspi = desktopSession._atspi()
	# libatspi marks every way to read a document's attributes deprecated, and offers no other.
	with warnings.catch_warnings():
		warnings.simplefilter("ignore", DeprecationWarning)
		return (Atspi.Document.get_document_attributes(document) or {}).get("DocURL", "")


def batches(engine: ModuleType, until: float, done) -> list:
	"""Batches from the engine until done(batches) or the monotonic time until."""
	got = []
	while not done(got) and (left := until - time.monotonic()) > 0:
		batch = engine.next(max(1, int(left * 1000)))
		if batch is not None:
			got.append(batch)
	return got


def test_basicPageRendersWholeAndInOrder(desktop: DesktopSession, engine: ModuleType, vbufLib: ModuleType):
	webSession.startFirefox(desktop, "basic.html")
	webSession.waitForDocument("Basic page")
	_document, bus, path = documentFor("Basic page")
	started = time.monotonic()
	rendered = engine.render(bus, path, DOC_HANDLE)
	took = time.monotonic() - started
	try:
		assert took < BASIC_RENDER_BOUND, rendered.info
		assert rendered.info["complete"] is True, rendered.info
		assert rendered.info["busy"] is False, rendered.info
		assert rendered.info["docURL"] == webSession.pageURL("basic.html"), rendered.info
		text = bufferText(vbufLib, rendered.stream)
		root = parseDocument(_units(rendered.stream))
	finally:
		rendered.stream.free()
	at = 0
	for expected in BASIC_TEXT:
		found = text.find(expected, at)
		assert found >= 0, f"{expected!r} not after offset {at} in {text!r}"
		at = found + len(expected)
	identifiers = [field.identifier for field in root.walk()]
	assert len(identifiers) == len(set(identifiers)), "an identifier is given twice"
	assert all(docHandle == DOC_HANDLE for docHandle, _ in identifiers)
	tables = [f for f in root.walk() if f.attributes.get("table-rowcount")]
	assert len(tables) == 2, [t.attributes for t in tables]
	layout, prices = tables
	assert layout.attributes.get("table-layout") == "1"
	assert "table-layout" not in prices.attributes
	assert (prices.attributes["table-rowcount"], prices.attributes["table-columncount"]) == ("3", "2")
	tableID = prices.attributes["table-id"]
	cells = [f for f in prices.walk() if "table-rownumber" in f.attributes]
	byText = {"".join(i[0] for i in cell.content if isinstance(i, tuple)): cell for cell in cells}
	where = {
		text: (cell.attributes["table-rownumber"], cell.attributes["table-columnnumber"])
		for text, cell in byText.items()
	}
	assert where == {
		"Fruit": ("1", "1"),
		"Price": ("1", "2"),
		"Apple": ("2", "1"),
		"2": ("2", "2"),
		"Pear": ("3", "1"),
		"3": ("3", "2"),
	}, where
	assert all(cell.attributes["table-id"] == tableID for cell in cells)
	priceHeader = byText["Price"].identifier[1]
	assert byText["3"].attributes["table-columnheadercells"] == f"{DOC_HANDLE},{priceHeader};"


def test_firstActionAfterARenderIsNotDropped(desktop: DesktopSession, engine: ModuleType):
	webSession.startFirefox(desktop, "basic.html")
	webSession.waitForDocument("Basic page")
	document, bus, path = documentFor("Basic page")
	rendered = engine.render(bus, path, DOC_HANDLE)
	rendered.stream.free()
	Atspi = desktopSession._atspi()
	link = named(document, Atspi.Role.LINK, "a link to the contact section")
	assert Atspi.Action.do_action(link, 0)
	target = webSession.pageURL("basic.html") + "#contact"
	deadline = time.monotonic() + 3
	while documentURL(document) != target:
		assert time.monotonic() < deadline, f"the address stayed {documentURL(document)!r}"
		time.sleep(0.05)
	got = batches(engine, time.monotonic() + 3, lambda b: any("urlChanged" in x.events for x in b))
	changed = [b for b in got if "urlChanged" in b.events]
	assert changed and changed[0].info["docURL"] == target, [b.info for b in got]


def test_eachChangeComesAsABatchThatMatchesAFreshRender(
	desktop: DesktopSession,
	engine: ModuleType,
	vbufLib: ModuleType,
):
	webSession.startFirefox(desktop, "dynamic.html")
	webSession.waitForDocument("Dynamic page")
	document, bus, path = documentFor("Dynamic page")
	Atspi = desktopSession._atspi()
	buttons = {name: named(document, Atspi.Role.BUTTON, name) for name in DYNAMIC_CHANGES}

	def fresh() -> tuple[Field, str]:
		rendered = engine.render(bus, path, DOC_HANDLE, progressive=False)
		try:
			units = _units(rendered.stream)
			text = bufferText(vbufLib, rendered.stream)
		finally:
			rendered.stream.free()
		# What changed while it rendered comes after it, and belongs to it.
		model = parseDocument(units)
		for batch in batches(engine, time.monotonic() + QUIET, lambda b: False):
			if batch.stream is not None:
				model = applyBatch(model, parseBatch(_units(batch.stream)))
				batch.stream.free()
		return model, text

	model, _ = fresh()
	kept = 0
	for name in DYNAMIC_CHANGES:
		pressed = time.monotonic()
		assert Atspi.Action.do_action(buttons[name], 0), name
		got = batches(engine, pressed + 5, lambda b: any(x.stream for x in b))
		took = time.monotonic() - pressed
		assert any(b.stream for b in got), f"{name}: no update: {[b.info for b in got]}"
		assert took < CHANGE_BOUND, f"{name}: the update took {took:.3f} s: {[b.info for b in got]}"
		while got:
			for batch in got:
				assert batch.docPath == path, batch.info
				if batch.stream is not None:
					replaced = parseBatch(_units(batch.stream))
					kept += sum(_keptIn(field) for _, field in replaced)
					model = applyBatch(model, replaced)
					batch.stream.free()
			got = batches(engine, time.monotonic() + QUIET, lambda b: bool(b))
		updated = bufferText(vbufLib, Units(encode(model, [])))
		model, expected = fresh()
		assert updated == expected, f"{name}: the updated buffer differs from a fresh render"
	assert kept, "no update kept an unchanged field as it was"


def test_followingALinkEndsTheOldDocument(desktop: DesktopSession, engine: ModuleType):
	webSession.startFirefox(desktop, "basic.html")
	webSession.waitForDocument("Basic page")
	document, bus, path = documentFor("Basic page")
	engine.render(bus, path, DOC_HANDLE).stream.free()
	Atspi = desktopSession._atspi()
	assert Atspi.Action.do_action(named(document, Atspi.Role.LINK, "The second page"), 0)

	def ended(got) -> bool:
		loaded = any("loadComplete" in b.events and b.docPath != path for b in got)
		return loaded and any("gone" in b.events and b.docPath == path for b in got)

	got = batches(engine, time.monotonic() + FOLLOW_BOUND, ended)
	assert ended(got), [b.info for b in got]
	webSession.waitForDocument("Second page")
	_second, _bus, secondPath = documentFor("Second page")
	assert any("loadComplete" in b.events and b.docPath == secondPath for b in got), [b.info for b in got]


def test_largePageComesTopFirstWithoutHoldingFirefox(
	desktop: DesktopSession,
	engine: ModuleType,
	vbufLib: ModuleType,
	tmp_path: Path,
):
	page = tmp_path / "large.html"
	subprocess.run([sys.executable, str(MAKE_PAGE), "large", str(page)], check=True)
	webSession.startFirefox(desktop, page.as_uri())
	webSession.waitForDocument(LARGE_TITLE, timeout=120)
	document, bus, path = documentFor(LARGE_TITLE)
	slowest = [0.0]
	stop = threading.Event()

	def probe() -> None:
		while not stop.is_set():
			started = time.monotonic()
			document.clear_cache()
			document.get_name()
			slowest[0] = max(slowest[0], time.monotonic() - started)
			time.sleep(0.02)

	prober = threading.Thread(target=probe, daemon=True)
	started = time.monotonic()
	prober.start()
	try:
		rendered = engine.render(bus, path, DOC_HANDLE)
		first = time.monotonic() - started
		got = batches(engine, started + LARGE_BOUND, lambda b: any("complete" in x.events for x in b))
		whole = time.monotonic() - started
	finally:
		stop.set()
		prober.join()
	try:
		assert first < FIRST_PART_BOUND, (first, rendered.info)
		assert rendered.info["complete"] is False, rendered.info
		top = bufferText(vbufLib, rendered.stream)
		assert LARGE_TITLE in top, top[:200]
		model = parseDocument(_units(rendered.stream))
	finally:
		rendered.stream.free()
	complete = [b for b in got if "complete" in b.events]
	assert complete, f"not complete within {LARGE_BOUND} s: {[b.info for b in got]}"
	assert whole < LARGE_BOUND
	assert slowest[0] < CALL_BOUND, f"a call to Firefox took {slowest[0]:.3f} s meanwhile"
	model = applyBatch(model, parseBatch(_units(complete[0].stream)))
	text = bufferText(vbufLib, Units(encode(model, [])))
	assert len(text) > 10 * len(top), (len(text), len(top))
	assert "Heading 999" in text
	for batch in got:
		if batch.stream is not None:
			batch.stream.free()


def test_tablesAreGeneratedFromNVDARoles(tmp_path: Path):
	generated = tmp_path / "tables.rs"
	subprocess.run(
		[sys.executable, str(ENGINE_CRATE / "genTables.py"), str(harness.ROOT), str(generated)],
		check=True,
		capture_output=True,
	)
	assert generated.read_text() == (ENGINE_CRATE / "src" / "tables.rs").read_text()
