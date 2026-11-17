"""A full-screen list in curses, as dialog, whiptail or a file manager draws one: a title, a list whose
highlighted item is drawn in reverse video, a status line, and an input line at the bottom.

Up and Down move the highlight and put the cursor on it, as those programs do; space marks or
unmarks the highlighted item, which the status line counts; typed letters go to the input line and
take the cursor there; Backspace takes one back; Escape or Return quits, printing what was
highlighted.
"""

import curses
import os

ITEMS = ("apple", "banana", "cherry", "damson", "elderberry", "fig", "grape", "hazelnut")
TITLE = "Fruit list"
INPUT_PROMPT = "Filter: "
FIRST_ITEM_ROW = 2


def statusText(marked: int) -> str:
	return f"{marked} marked"


def itemText(index: int, marked: set[int]) -> str:
	return f"{'*' if index in marked else ' '} {ITEMS[index]}"


def draw(screen, highlighted: int, marked: set[int], typed: str, cursorInInput: bool) -> None:
	height, width = screen.getmaxyx()
	screen.erase()
	screen.addstr(0, 0, TITLE[: width - 1])
	for index in range(len(ITEMS)):
		attribute = curses.A_REVERSE if index == highlighted else curses.A_NORMAL
		screen.addstr(FIRST_ITEM_ROW + index, 0, itemText(index, marked), attribute)
	screen.addstr(height - 2, 0, statusText(len(marked)))
	screen.addstr(height - 1, 0, INPUT_PROMPT + typed)
	if cursorInInput:
		screen.move(height - 1, len(INPUT_PROMPT) + len(typed))
	else:
		screen.move(FIRST_ITEM_ROW + highlighted, 0)
	screen.refresh()


def run(screen) -> str:
	curses.curs_set(1)
	highlighted = 0
	marked: set[int] = set()
	typed = ""
	cursorInInput = False
	while True:
		draw(screen, highlighted, marked, typed, cursorInInput)
		key = screen.get_wch()
		if key in ("\x1b", "\n") or key == curses.KEY_ENTER:
			return ITEMS[highlighted]
		if key in (curses.KEY_UP, curses.KEY_DOWN):
			step = -1 if key == curses.KEY_UP else 1
			highlighted = min(len(ITEMS) - 1, max(0, highlighted + step))
			cursorInInput = False
		elif key == " ":
			marked ^= {highlighted}
		elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
			typed = typed[:-1]
			cursorInInput = True
		elif isinstance(key, str) and key.isprintable():
			typed += key
			cursorInInput = True


def main() -> None:
	# Escape alone otherwise waits a second for the rest of a sequence that is not coming.
	os.environ.setdefault("ESCDELAY", "25")
	chosen = curses.wrapper(run)
	print(f"chose {chosen}")


if __name__ == "__main__":
	main()
