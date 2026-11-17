"""A Qt 6 window holding the same controls as gtkFixture.py. Rig only."""

import os
import sys

# The rig passes no QT_ variables in; Qt's AT-SPI bridge otherwise waits for the bus's IsEnabled.
os.environ.setdefault("QT_LINUX_ACCESSIBILITY_ALWAYS_ON", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
	QApplication,
	QCheckBox,
	QComboBox,
	QFormLayout,
	QGroupBox,
	QLabel,
	QLineEdit,
	QListWidget,
	QMainWindow,
	QPushButton,
	QRadioButton,
	QSlider,
	QSpinBox,
	QSplitter,
	QTableWidget,
	QTableWidgetItem,
	QTabWidget,
	QTextEdit,
	QTreeWidget,
	QTreeWidgetItem,
	QVBoxLayout,
	QWidget,
)

app = QApplication(sys.argv)


def buildChat() -> QMainWindow:
	"""A conferencing client's window, laid out as TeamTalk's is: tabs whose current tab Qt keeps marking
	focused, a read-only history, a channel tree with columns that has the focus at start, a grid, a
	group box whose controls Qt also labels by the box, and a menu whose first item is disabled."""
	chat = QMainWindow()
	chat.setWindowTitle("Qt chat window")
	channels = chat.menuBar().addMenu("&Channels")
	join = channels.addAction("&Join Channel")
	join.setEnabled(False)
	channels.addAction("Channel &Information")
	channels.addAction("&Leave Channel")
	chat.menuBar().addMenu("&Help").addAction("&About")
	splitter = QSplitter()
	tabs = QTabWidget()
	chatPage = QWidget()
	chatLayout = QVBoxLayout(chatPage)
	history = QTextEdit()
	history.setReadOnly(True)
	history.setAccessibleName("History")
	history.setPlainText("Alice joined the channel.\nBob: hello everyone")
	chatLayout.addWidget(history)
	message = QLineEdit()
	message.setAccessibleName("Message")
	chatLayout.addWidget(message)
	chatLayout.addWidget(QPushButton("&Send"))
	settings = QGroupBox("User Settings")
	settingsLayout = QFormLayout(settings)
	gender = QComboBox()
	gender.addItems(["Neutral", "Male", "Female"])
	settingsLayout.addRow("&Gender:", gender)
	chatLayout.addWidget(settings)
	tabs.addTab(chatPage, "C&hat")
	grid = QTableWidget(2, 3)
	grid.setAccessibleName("Grid")
	grid.setHorizontalHeaderLabels(["Name", "Size", "Kind"])
	for row, values in enumerate((("a.txt", "6 bytes", "text"), ("b.png", "2 KB", "image"))):
		for column, value in enumerate(values):
			grid.setItem(row, column, QTableWidgetItem(value))
	tabs.addTab(grid, "&Files")
	splitter.addWidget(tabs)
	tree = QTreeWidget()
	tree.setAccessibleName("Channel list")
	tree.setHeaderLabels(["Name", "Users"])
	lobby = QTreeWidgetItem(tree, ["Lobby", "2"])
	QTreeWidgetItem(lobby, ["Alice", ""])
	QTreeWidgetItem(lobby, ["Bob", ""])
	QTreeWidgetItem(tree, ["Music", "0"])
	tree.setCurrentItem(lobby)
	splitter.addWidget(tree)
	chat.setCentralWidget(splitter)
	chat.resize(700, 500)
	chat.show()
	tree.setFocus()
	return chat


if sys.argv[1:] == ["chat"]:
	win = buildChat()

	def announceChat():
		win.activateWindow()
		print("ready", flush=True)

	QTimer.singleShot(300, announceChat)
	sys.exit(app.exec())

win = QMainWindow()
win.setWindowTitle("Qt fixture window")
fileMenu = win.menuBar().addMenu("&File")
openAction = QAction("&Open", win)
openAction.setShortcut(QKeySequence("Ctrl+O"))
fileMenu.addAction(openAction)
recent = fileMenu.addMenu("&Recent")
recent.addAction("one.txt")
wrap = QAction("&Wrap lines", win, checkable=True)
fileMenu.addAction(wrap)
fileMenu.addSeparator()
fileMenu.addAction("&Quit")
win.menuBar().addMenu("&Help").addAction("About")

central = QWidget()
layout = QVBoxLayout(central)
button = QPushButton("&Press me")
button.setToolTip("Does nothing")
layout.addWidget(button)
layout.addWidget(QCheckBox("&Enable sound"))
group = QGroupBox("Size")
groupLayout = QVBoxLayout(group)
radios = [QRadioButton(t) for t in ("Small", "Medium", "Large")]
for r in radios:
	groupLayout.addWidget(r)
radios[1].setChecked(True)
layout.addWidget(group)
form = QFormLayout()
combo = QComboBox()
combo.addItems(["Red", "Green", "Blue"])
combo.setCurrentIndex(1)
form.addRow("&Colour:", combo)
entry = QLineEdit("Alice")
form.addRow("&Name:", entry)
slider = QSlider(Qt.Orientation.Horizontal)
slider.setRange(0, 100)
slider.setValue(40)
form.addRow("&Volume:", slider)
spin = QSpinBox()
spin.setRange(0, 10)
spin.setValue(3)
form.addRow("C&ount:", spin)
fruit = QListWidget()
fruit.addItems(
	["Apples", "Pears", "Plums"]
	+ [f"Item {n}" for n in range(int(os.environ.get("LINDA_LISTSIZE", "0")))]
)
fruit.setCurrentRow(0)
form.addRow("Fr&uit:", fruit)
layout.addLayout(form)
tabs = QTabWidget()
tabs.addTab(QLabel("First page"), "General")
tabs.addTab(QLabel("Second page"), "Advanced")
layout.addWidget(tabs)
win.setCentralWidget(central)
win.statusBar().showMessage("Ready")
win.resize(500, 600)
win.show()
button.setFocus()


def announce():
	win.activateWindow()
	print("ready", flush=True)


QTimer.singleShot(300, announce)
sys.exit(app.exec())
