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
	QTabWidget,
	QVBoxLayout,
	QWidget,
)

app = QApplication(sys.argv)
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
	+ [f"Item {n}" for n in range(int(os.environ.get("BASEDREADER_LISTSIZE", "0")))]
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
