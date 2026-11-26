"""Regression tests for 2D tables and spreadsheets (e.g. LibreOffice Calc) vs GTK 3 tree views.

LibreOffice Calc under GTK 3 reports its sheet as a table with manages-descendants under a
document spreadsheet ancestor, and its cells as table cells named by their coordinates ("A1",
"B1", ...). Unlike a native GtkTreeView list, a spreadsheet or document table must keep
Role.TABLE / Role.TABLECELL and announce only the active cell, not multiple columns across
the row.
"""

from pathlib import Path

import harness

SCRIPT = """
from dataclasses import dataclass, field

import NVDAHelper  # noqa: F401
import characterProcessing
import config
import languageHandler
import nvwave
import speech
import speechDictHandler
import textInfos
from controlTypes import OutputReason, Role, State
from linuxPlatform.atspi import client as atspiClient
from linuxPlatform.atspi import handler
from linuxPlatform.atspi.client import Node
from NVDAObjects.AtSpi import AtSpi, SpreadsheetCell, TableCell, TableRowItem
from NVDAObjects.AtSpi import textInfo as atspiTextInfoModule
from NVDAObjects.AtSpi.deletion import DeletionEcho
from NVDAObjects.behaviors import EditableTextWithAutoSelectDetection

languageHandler.setLanguage("en")
nvwave.initialize()
speechDictHandler.initialize()
characterProcessing.initialize()
config.conf["speech"]["synth"] = "silence"
speech.initialize()


@dataclass
class FakeInfo:
	role: str
	name: str = ""
	toolkit: str = "gtk"
	states: frozenset[str] = frozenset({"enabled", "sensitive", "showing", "visible"})
	interfaces: frozenset[str] = frozenset({"Accessible", "Component"})
	attributes: dict[str, str] = field(default_factory=dict)
	parent: Node | None = None
	children: list[Node] = field(default_factory=list)
	tableSize: tuple[int, int] = (0, 0)
	cells: dict[tuple[int, int], Node] = field(default_factory=dict)
	columnHeaders: dict[int, str] = field(default_factory=dict)
	cellPos: tuple[int, int] = (-1, -1)
	cellTable: Node | None = None
	selectedCount: int = 0
	text: str = ""
	caretOffset: int = -1


class FakeClient:
	def __init__(self) -> None:
		self.nodes: dict[Node, FakeInfo] = {}
		self._apps: list[Node] = []

	def add(self, path: str, info: FakeInfo) -> Node:
		node = Node(":1.99", path, None)
		self.nodes[node] = info
		if info.role == "application":
			self._apps.append(node)
		if info.parent is not None:
			self.nodes[info.parent].children.append(node)
		return node

	def applications(self) -> list[Node]:
		return list(self._apps)

	def isHung(self, busName: str) -> bool:
		return False

	def role(self, node: Node) -> str:
		return self.nodes[node].role

	def name(self, node: Node) -> str:
		return self.nodes[node].name

	def description(self, node: Node) -> str:
		return ""

	def toolkit(self, node: Node) -> str:
		return self.nodes[node].toolkit

	def accessibleId(self, node: Node) -> str:
		return ""

	def states(self, node: Node) -> frozenset[str]:
		return self.nodes[node].states

	def interfaces(self, node: Node) -> frozenset[str]:
		return self.nodes[node].interfaces

	def attributes(self, node: Node) -> dict[str, str]:
		return self.nodes[node].attributes

	def relations(self, node: Node) -> list:
		return []

	def parent(self, node: Node) -> Node | None:
		return self.nodes[node].parent

	def children(self, node: Node) -> list[Node]:
		return list(self.nodes[node].children)

	def childCount(self, node: Node) -> int:
		return len(self.nodes[node].children)

	def childAt(self, node: Node, index: int) -> Node | None:
		kids = self.nodes[node].children
		return kids[index] if 0 <= index < len(kids) else None

	def indexInParent(self, node: Node) -> int:
		parent = self.nodes[node].parent
		if parent is None:
			return -1
		kids = self.nodes[parent].children
		return kids.index(node) if node in kids else -1

	def tableSize(self, node: Node) -> tuple[int, int]:
		return self.nodes[node].tableSize

	def cellAt(self, node: Node, row: int, column: int) -> Node | None:
		return self.nodes[node].cells.get((row, column))

	def cellPosition(self, node: Node) -> tuple[int, int]:
		return self.nodes[node].cellPos

	def cellTable(self, node: Node) -> Node | None:
		return self.nodes[node].cellTable

	def columnHeader(self, node: Node, column: int) -> str:
		return self.nodes[node].columnHeaders.get(column, "")

	def columnHeaderNames(self, node: Node) -> list[str]:
		return []

	def selectedCount(self, node: Node) -> int:
		return self.nodes[node].selectedCount

	def processID(self, node: Node) -> int:
		import os

		return os.getpid()

	def text(self, node: Node, start: int = 0, end: int = -1) -> str:
		t = self.nodes[node].text
		return t[start:] if end < 0 else t[start:end]

	def characterCount(self, node: Node) -> int:
		return len(self.nodes[node].text)

	def caretOffset(self, node: Node) -> int:
		return self.nodes[node].caretOffset

	def selections(self, node: Node) -> list[tuple[int, int]]:
		return []

	def stringAtOffset(self, node: Node, offset: int, granularity: str) -> tuple[int, int, str]:
		t = self.nodes[node].text
		return (0, len(t), t)

	def textAttributes(self, node: Node, offset: int) -> tuple[int, int, dict[str, str]]:
		t = self.nodes[node].text
		return (0, len(t), {})

	def defaultTextAttributes(self, node: Node) -> dict[str, str]:
		return {}

	def extents(self, node: Node):
		return (0, 0, 100, 20)


fake = FakeClient()


class FakeTextSource:
	def __init__(self, node: Node, unit: str) -> None:
		self.node = node
		self.unit = unit

	def characterCount(self) -> int:
		return fake.characterCount(self.node)

	def text(self, start: int, end: int) -> str:
		return fake.text(self.node, start, end)

	def stringAtOffset(self, offset: int, granularity: str) -> tuple[int, int, str]:
		return fake.stringAtOffset(self.node, offset, granularity)

	def caretOffset(self) -> int:
		return fake.caretOffset(self.node)

	def selections(self) -> list[tuple[int, int]]:
		return fake.selections(self.node)

	def attributeRun(self, offset: int) -> tuple[dict[str, str], int, int]:
		t = fake.nodes[self.node].text
		return ({}, 0, len(t))

	def defaultAttributes(self) -> dict[str, str]:
		return {}


atspiClient.client = fake
atspiTextInfoModule.TextSource = FakeTextSource
handler.registerObject = lambda obj: None
handler.desktopNode = lambda: Node(":1.0", "/org/a11y/atspi/accessible/root", None)
handler.getObject = lambda node: AtSpi(node=node)

app = fake.add("/app", FakeInfo(role="application", name="soffice.bin"))
frame = fake.add("/frame", FakeInfo(role="frame", name="Untitled 1 - LibreOffice Calc", parent=app))

# 1. LibreOffice Calc spreadsheet: document spreadsheet -> table (1048576x16384, manages-descendants, no toolkit attr)
doc = fake.add("/calc/doc", FakeInfo(role="document spreadsheet", name="Untitled 1", parent=frame))
sheet = fake.add(
	"/calc/sheet",
	FakeInfo(
		role="table",
		name="Sheet1",
		states=frozenset({"enabled", "focusable", "focused", "manages-descendants", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "Selection", "Table"}),
		attributes={},
		parent=doc,
		tableSize=(1048576, 16384),
	),
)
cellStates = frozenset(
	{"editable", "enabled", "focusable", "focused", "multi-line", "selectable", "sensitive", "showing", "transient", "visible"}
)
cellIfaces = frozenset({"Accessible", "Component", "TableCell", "Text", "Value"})
calcCells = {}
for rowIdx, rowNum in enumerate((1, 2)):
	for colIdx, colLetter in enumerate(("A", "B", "C", "D")):
		coord = f"{colLetter}{rowNum}"
		text = ""
		attrs = {}
		if coord == "B1":
			text = "Total"
		elif coord == "B2":
			text = "42"
			attrs = {"Formula": "=SUM(A1:A10)"}
		cellNode = fake.add(
			f"/calc/cell/{coord}",
			FakeInfo(
				role="table cell",
				name=coord,
				states=cellStates,
				interfaces=cellIfaces,
				attributes=attrs,
				parent=sheet,
				cellPos=(rowIdx, colIdx),
				cellTable=sheet,
				text=text,
			),
		)
		fake.nodes[sheet].cells[(rowIdx, colIdx)] = cellNode
		calcCells[coord] = cellNode

# 1b. A spreadsheet table that ALSO sets toolkit=gtk (e.g. a native GTK widget inside document spreadsheet)
doc2 = fake.add("/calc2/doc", FakeInfo(role="document spreadsheet", name="Sheet doc", parent=frame))
sheet2 = fake.add(
	"/calc2/sheet",
	FakeInfo(
		role="table",
		name="Sheet2",
		states=frozenset({"enabled", "focusable", "manages-descendants", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "Table"}),
		attributes={"toolkit": "gtk"},
		parent=doc2,
		tableSize=(100, 10),
	),
)
sheet2Cell = fake.add(
	"/calc2/cell/A1",
	FakeInfo(
		role="table cell",
		name="A1",
		states=cellStates,
		interfaces=cellIfaces,
		parent=sheet2,
		cellPos=(0, 0),
		cellTable=sheet2,
		text="Hello",
	),
)
fake.nodes[sheet2].cells[(0, 0)] = sheet2Cell

# 1c. A wide >64 column GTK table without a document ancestor
wideGrid = fake.add(
	"/wide/grid",
	FakeInfo(
		role="table",
		name="Grid",
		states=frozenset({"enabled", "focusable", "manages-descendants", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "Table"}),
		attributes={"toolkit": "gtk"},
		parent=frame,
		tableSize=(500, 256),
	),
)
wideCell = fake.add(
	"/wide/cell/A1",
	FakeInfo(
		role="table cell",
		name="A1",
		states=frozenset({"enabled", "focusable", "focused", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "TableCell"}),
		parent=wideGrid,
		cellPos=(0, 0),
		cellTable=wideGrid,
	),
)
fake.nodes[wideGrid].cells[(0, 0)] = wideCell

# 1d. Edit mode paragraph inside Calc spreadsheet (must still get EditableTextWithAutoSelectDetection)
editPara = fake.add(
	"/calc/edit",
	FakeInfo(
		role="paragraph",
		name="",
		states=frozenset({"editable", "enabled", "focusable", "focused", "multi-line", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "Text"}),
		parent=doc,
		text="Editing",
		caretOffset=7,
	),
)

# 2. LibreOffice Writer 3x3 document table (small column count, inside document text, no toolkit attr)
writerDoc = fake.add("/writer/doc", FakeInfo(role="document text", name="Document", parent=frame))
writerTable = fake.add(
	"/writer/table",
	FakeInfo(
		role="table",
		name="Table1",
		states=frozenset({"enabled", "focusable", "manages-descendants", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "Table"}),
		attributes={},
		parent=writerDoc,
		tableSize=(3, 3),
	),
)
writerCell0 = fake.add(
	"/writer/c0",
	FakeInfo(
		role="table cell",
		name="First",
		states=frozenset({"enabled", "focusable", "focused", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "TableCell"}),
		parent=writerTable,
		cellPos=(0, 0),
		cellTable=writerTable,
	),
)
writerCell1 = fake.add(
	"/writer/c1",
	FakeInfo(
		role="table cell",
		name="Second",
		states=frozenset({"enabled", "focusable", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "TableCell"}),
		parent=writerTable,
		cellPos=(0, 1),
		cellTable=writerTable,
	),
)
fake.nodes[writerTable].cells[(0, 0)] = writerCell0
fake.nodes[writerTable].cells[(0, 1)] = writerCell1

# 3. Native GTK 3 GtkTreeView (3-column list view & 1-column tree view)
gtkPanel = fake.add("/gtk/panel", FakeInfo(role="filler", parent=frame))
listView = fake.add(
	"/gtk/list",
	FakeInfo(
		role="table",
		name="Files",
		states=frozenset({"enabled", "focusable", "manages-descendants", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "Selection", "Table"}),
		attributes={"toolkit": "gtk"},
		parent=gtkPanel,
		tableSize=(2, 3),
		columnHeaders={1: "Name", 2: "Size"},
	),
)
lv0 = fake.add(
	"/gtk/list/c0",
	FakeInfo(
		role="table cell",
		name="",
		states=frozenset({"checked", "enabled", "focusable", "selectable", "selected", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "TableCell"}),
		parent=listView,
		cellPos=(0, 0),
		cellTable=listView,
	),
)
lv1 = fake.add(
	"/gtk/list/c1",
	FakeInfo(
		role="table cell",
		name="a.txt",
		states=frozenset({"enabled", "focusable", "focused", "selectable", "selected", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "TableCell", "Text"}),
		parent=listView,
		cellPos=(0, 1),
		cellTable=listView,
	),
)
lv2 = fake.add(
	"/gtk/list/c2",
	FakeInfo(
		role="table cell",
		name="6 bytes",
		states=frozenset({"enabled", "focusable", "selectable", "selected", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "TableCell", "Text"}),
		parent=listView,
		cellPos=(0, 2),
		cellTable=listView,
	),
)
fake.nodes[listView].cells[(0, 0)] = lv0
fake.nodes[listView].cells[(0, 1)] = lv1
fake.nodes[listView].cells[(0, 2)] = lv2

treeView = fake.add(
	"/gtk/tree",
	FakeInfo(
		role="tree table",
		name="Folders",
		states=frozenset({"enabled", "focusable", "manages-descendants", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "Selection", "Table"}),
		attributes={"toolkit": "gtk"},
		parent=gtkPanel,
		tableSize=(5, 1),
	),
)
tv0 = fake.add(
	"/gtk/tree/c0",
	FakeInfo(
		role="table cell",
		name="Home",
		states=frozenset({"enabled", "focusable", "focused", "selectable", "selected", "sensitive", "showing", "visible"}),
		interfaces=frozenset({"Accessible", "Component", "TableCell"}),
		parent=treeView,
		cellPos=(0, 0),
		cellTable=treeView,
	),
)
fake.nodes[treeView].cells[(0, 0)] = tv0


def spokenText(obj) -> list[str]:
	return [item for item in speech.getObjectSpeech(obj, reason=OutputReason.FOCUS) if isinstance(item, str)]


sheetObj = AtSpi(node=sheet)
a1Obj = AtSpi(node=calcCells["A1"])
b1Obj = AtSpi(node=calcCells["B1"])
b2Obj = AtSpi(node=calcCells["B2"])
c2Obj = AtSpi(node=calcCells["C2"])

a1Speech = spokenText(a1Obj)
b1Speech = spokenText(b1Obj)
b2Speech = spokenText(b2Obj)
c2Speech = spokenText(c2Obj)

# Verify review TextInfo on populated cell B1
b1Info = b1Obj.makeTextInfo(textInfos.POSITION_FIRST)
b1Info.expand(textInfos.UNIT_LINE)
b1LineText = b1Info.text

# Verify multi-cell selection reports State.SELECTED
fake.nodes[sheet].selectedCount = 2
b1MultiObj = AtSpi(node=calcCells["B1"])
b1MultiSelected = State.SELECTED in b1MultiObj.states
fake.nodes[sheet].selectedCount = 0

sheet2Obj = AtSpi(node=sheet2)
sheet2CellObj = AtSpi(node=sheet2Cell)
wideGridObj = AtSpi(node=wideGrid)
wideCellObj = AtSpi(node=wideCell)
editParaObj = AtSpi(node=editPara)
writerTableObj = AtSpi(node=writerTable)
writerCellObj = AtSpi(node=writerCell0)
listViewObj = AtSpi(node=listView)
listItemObj = AtSpi(node=lv1)
treeViewObj = AtSpi(node=treeView)
treeItemObj = AtSpi(node=tv0)

result = {
	"sheetRole": sheetObj.role.name,
	"a1Role": a1Obj.role.name,
	"a1IsSpreadsheetCell": isinstance(a1Obj, SpreadsheetCell),
	"a1IsTableCell": isinstance(a1Obj, TableCell),
	"a1IsRowItem": isinstance(a1Obj, TableRowItem),
	"a1IsEditable": isinstance(a1Obj, EditableTextWithAutoSelectDetection),
	"a1IsDeletionEcho": isinstance(a1Obj, DeletionEcho),
	"a1Speech": a1Speech,
	"b1Speech": b1Speech,
	"b2Speech": b2Speech,
	"c2Speech": c2Speech,
	"b1LineText": b1LineText,
	"b1MultiSelected": b1MultiSelected,
	"sheet2Role": sheet2Obj.role.name,
	"sheet2CellRole": sheet2CellObj.role.name,
	"sheet2CellSpeech": spokenText(sheet2CellObj),
	"wideGridRole": wideGridObj.role.name,
	"wideCellRole": wideCellObj.role.name,
	"wideCellIsRowItem": isinstance(wideCellObj, TableRowItem),
	"editParaIsEditable": isinstance(editParaObj, EditableTextWithAutoSelectDetection),
	"writerTableRole": writerTableObj.role.name,
	"writerCellRole": writerCellObj.role.name,
	"writerCellIsRowItem": isinstance(writerCellObj, TableRowItem),
	"writerCellIsSpreadsheet": isinstance(writerCellObj, SpreadsheetCell),
	"writerCellSpeech": spokenText(writerCellObj),
	"listViewRole": listViewObj.role.name,
	"listItemRole": listItemObj.role.name,
	"listItemIsRowItem": isinstance(listItemObj, TableRowItem),
	"listItemName": listItemObj.name,
	"treeViewRole": treeViewObj.role.name,
	"treeItemRole": treeItemObj.role.name,
	"treeItemIsRowItem": isinstance(treeItemObj, TableRowItem),
}
"""


def test_spreadsheetAndDocumentTablesAnnounceOnlyActiveCell(tmp_path: Path):
	done = harness.runChild(tmp_path, SCRIPT, timeout=30)
	done.assertClean()
	v = done.value

	# LibreOffice Calc sheet and cells
	assert v["sheetRole"] == "TABLE", v
	assert v["a1Role"] == "TABLECELL", v
	assert v["a1IsSpreadsheetCell"] is True, v
	assert v["a1IsTableCell"] is True, v
	assert v["a1IsRowItem"] is False, v
	assert v["a1IsEditable"] is False, v
	assert v["a1IsDeletionEcho"] is False, v

	# Moving across Calc cells announces only the active cell, never adjacent coordinates or noisy states.
	assert v["a1Speech"] == ["A1"], v["a1Speech"]
	assert v["b1Speech"] == ["Total", "B1"], v["b1Speech"]
	assert v["b1LineText"] == "Total", v

	assert "B2" in v["b2Speech"] and "42" in v["b2Speech"], v["b2Speech"]
	assert any("formula" in part.lower() for part in v["b2Speech"]), v["b2Speech"]
	assert all("A2" not in part and "C2" not in part for part in v["b2Speech"]), v["b2Speech"]

	assert v["c2Speech"] == ["C2"], v["c2Speech"]

	assert v["b1MultiSelected"] is True, v

	# Spreadsheet table with toolkit=gtk still stays TABLE / TABLECELL
	assert v["sheet2Role"] == "TABLE", v
	assert v["sheet2CellRole"] == "TABLECELL", v
	assert "A1" in v["sheet2CellSpeech"] and "Hello" in v["sheet2CellSpeech"], v["sheet2CellSpeech"]

	# Wide grid (>64 columns) without document ancestor stays TABLE / TABLECELL
	assert v["wideGridRole"] == "TABLE", v
	assert v["wideCellRole"] == "TABLECELL", v
	assert v["wideCellIsRowItem"] is False, v

	# Cell edit paragraph still receives EditableTextWithAutoSelectDetection
	assert v["editParaIsEditable"] is True, v

	# Writer 3x3 document table stays TABLE / TABLECELL, not TableRowItem
	assert v["writerTableRole"] == "TABLE", v
	assert v["writerCellRole"] == "TABLECELL", v
	assert v["writerCellIsRowItem"] is False, v
	assert v["writerCellIsSpreadsheet"] is False, v
	assert "First" in v["writerCellSpeech"] and all("Second" not in p for p in v["writerCellSpeech"]), v[
		"writerCellSpeech"
	]

	# Native GTK 3 GtkTreeView list & tree still keep LIST/LISTITEM and TREEVIEW/TREEVIEWITEM
	assert v["listViewRole"] == "LIST", v
	assert v["listItemRole"] == "LISTITEM", v
	assert v["listItemIsRowItem"] is True, v
	assert v["listItemName"] == "a.txt; Size: 6 bytes", v
	assert v["treeViewRole"] == "TREEVIEW", v
	assert v["treeItemRole"] == "TREEVIEWITEM", v
	assert v["treeItemIsRowItem"] is True, v
