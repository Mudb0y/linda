"""An imitation of a coding assistant's text interface, drawn as programs built on a React-style
terminal renderer draw one: finished parts of the conversation are written once and scroll away,
and below them a live region is erased and written again on every change.

terminalAssistant.py [--cursor=drawn|real] [--render=full|lines] [--seed=N] [--log=PATH]

The live region holds the reply while it streams, a few words at a time and wrapped again as it
grows; a status row whose spinner, word and counters change several times a second; an input box
drawn with box-drawing characters, with a prompt character and the text being typed; and a hint row.

--cursor=drawn hides the terminal's cursor and draws one as a reversed cell in the input box, leaving
the real one under the live region; --cursor=real leaves the terminal's cursor in the input box, or
on the marked option of a menu. --render=full erases the whole live region and writes it again, in
the three writes such a renderer makes when it also adds finished text; --render=lines rewrites only
the rows that changed. --log, or BASEDREADER_ASSISTANT_LOG, names a file to which a JSON line with
time.monotonic() is added as each paragraph of the reply starts to appear.

Submitting any text plays the conversation in REPLY: a reply, a tool call with its output, a
question with numbered options marked by "❯" that Up and Down move, and a last reply. Submitting
"wait" first shows only the status row for WAIT_SECONDS. "/exit" or Control+D on an empty line
quits.
"""

import json
import os
import random
import select
import sys
import termios
import textwrap
import time
import tty

#: The conversation, in order: ("say", paragraph) starts a reply with its mark, ("more", paragraph)
#: goes on with it, ("code", lines), ("tool", command, output lines), ("ask", question rows,
#: options), ("think", seconds).
REPLY = (
	("think", 0.8),
	(
		"say",
		(
			"I will look at the files in this folder first, so that I can tell you what each one is for "
			"and which of them matter for the change you asked about."
		),
	),
	("tool", "Run(ls)", ("notes.txt", "main.py", "config.toml")),
	("say", "Here is what each file does:"),
	("more", "1. notes.txt holds the meeting notes from last week."),
	("more", "2. main.py starts the program and reads its settings."),
	("more", "3. config.toml sets the port and the log level."),
	("more", "The program starts like this:"),
	("code", ("def main():", "    settings = load()", "    serve(settings)")),
	(
		"ask",
		("Run command", "", "  rm notes.txt", "", "Do you want to proceed?"),
		("Yes", "Yes, and do not ask again for this command", "No, and tell me what to do differently"),
	),
	("tool", "Run(rm notes.txt)", ("removed notes.txt",)),
	("say", "Done. The folder now holds two files, and the program is unchanged."),
)
#: How long "wait" shows only the status row.
WAIT_SECONDS = 4.0
SPINNER = ("·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢")
SPINNER_SECONDS = 0.1
WORDS = ("Thinking", "Pondering", "Brewing", "Musing")
WORD_SECONDS = 2.0
#: Seconds between pieces of a streamed reply, and words in a piece.
PIECE_SECONDS = (0.04, 0.1)
PIECE_WORDS = (1, 3)
IDLE_HINT = "  ? for shortcuts"
MENU_HINT = "  Enter to confirm · Esc to cancel"
BUSY_HINT = "  esc to interrupt · ctrl+t to show todos"
MARK = "❯"
REPLY_MARK = "●"
OUTPUT_MARK = "⎿"
PROMPT = "> "

ESC = "\x1b"
HIDE_CURSOR = ESC + "[?25l"
SHOW_CURSOR = ESC + "[?25h"
ERASE_LINE = ESC + "[2K"
REVERSE, NO_REVERSE = ESC + "[7m", ESC + "[27m"
DIM, NORMAL = ESC + "[2m", ESC + "[22m"


def heard(prompt: str, chosen: int) -> tuple[list[str], list[str]]:
	"""What a listener should hear of the conversation, as lines without their marks and framing: from
	submitting prompt until the question's options, and after choosing option chosen."""
	before, after = [prompt], []
	lines = before
	options: tuple[str, ...] = ()
	for part in REPLY:
		kind = part[0]
		if kind in ("say", "more"):
			lines.append(part[1])
		elif kind == "code":
			lines += part[1]
		elif kind == "tool":
			if lines is after and chosen == len(options) - 1:
				lines.append("Declined")
				continue
			lines += [part[1], "Running…", *part[2]]
		elif kind == "ask":
			options = part[2]
			lines += [row.strip() for row in part[1] if row.strip()]
			lines += [f"{index + 1}. {option}" for index, option in enumerate(options)]
			lines.append(MENU_HINT.strip())
			lines = after
	return before, after


def eraseLines(count: int) -> str:
	"""The sequence such a renderer erases its previous output with, from the row under it."""
	if not count:
		return ""
	return (ERASE_LINE + ESC + "[1A") * (count - 1) + ERASE_LINE + ESC + "[G"


def visibleLength(text: str) -> int:
	length, inEscape = 0, False
	for ch in text:
		if ch == ESC:
			inEscape = True
		elif inEscape:
			inEscape = not ch.isalpha()
		else:
			length += 1
	return length


class Assistant:
	def __init__(self, cursor: str, render: str, seed: int, logPath: str | None = None) -> None:
		self.logPath = logPath
		self.cursor = cursor
		self.render = render
		self.random = random.Random(seed)
		self.width = os.get_terminal_size(sys.stdout.fileno()).columns
		self.typed = ""
		self.caret = 0
		self.history: list[str] = []
		#: Rows written for good and not yet put on the screen.
		self.finished: list[str] = []
		self.frameRows = 0
		#: The frame's row and column of the real cursor, or None for the row under the frame.
		self.cursorAt: tuple[int, int] | None = None
		self.busySince: float | None = None
		self.tokens = 0
		self.streaming = ""
		self.streamPrefix = REPLY_MARK
		self.running: str | None = None
		self.question: tuple[str, tuple[str, ...]] | None = None
		self.choice = 0
		self.chosen: int | None = None
		self.script: list = []
		self.waitUntil = 0.0
		self.pieces: list[str] = []
		self.nextPieceAt = 0.0
		self.done = False
		self.paragraphStarted = False
		self.paragraph = ""

	# Drawing

	def write(self, text: str) -> None:
		os.write(sys.stdout.fileno(), text.encode())

	def log(self, event: str, text: str) -> None:
		if self.logPath:
			with open(self.logPath, "a") as log:
				log.write(json.dumps({"t": time.monotonic(), "event": event, "text": text}) + "\n")

	def wrap(self, text: str, mark: str) -> list[str]:
		return textwrap.wrap(
			text,
			width=self.width - 1,
			initial_indent=f"{mark} ",
			subsequent_indent="  ",
			break_on_hyphens=False,
		) or [f"{mark} "]

	def boxed(self, rows: list[str], cursorRow: int | None = None, cursorColumn: int = 0) -> list[str]:
		inner = self.width - 4
		out = ["╭" + "─" * (self.width - 2) + "╮"]
		for row in rows:
			out.append("│ " + row + " " * max(0, inner - visibleLength(row)) + " │")
		out.append("╰" + "─" * (self.width - 2) + "╯")
		return out

	def statusRow(self) -> str:
		elapsed = time.monotonic() - self.busySince
		glyph = SPINNER[int(elapsed / SPINNER_SECONDS) % len(SPINNER)]
		word = WORDS[int(elapsed / WORD_SECONDS) % len(WORDS)]
		tokens = f"{self.tokens / 1000:.1f}k" if self.tokens >= 1000 else str(self.tokens)
		return f"{glyph} {word}… ({int(elapsed)}s · ↓ {tokens} tokens · esc to interrupt)"

	def inputRow(self) -> str:
		if self.cursor == "drawn":
			under = self.typed[self.caret] if self.caret < len(self.typed) else " "
			text = self.typed[: self.caret] + REVERSE + under + NO_REVERSE + self.typed[self.caret + 1 :]
		else:
			text = self.typed
		return PROMPT + text

	def frame(self) -> list[str]:
		"""The live region's rows, and where the real cursor goes in it."""
		rows: list[str] = []
		if self.streaming:
			rows += self.wrap(self.streaming, self.streamPrefix)
		if self.running is not None:
			rows += self.wrap(self.running, REPLY_MARK)
			rows.append(f"  {OUTPUT_MARK}  Running…")
		rows.append("")
		self.cursorAt = None
		if self.question is not None:
			question, options = self.question
			body = [*question, ""]
			for index, option in enumerate(options):
				body.append(f"{MARK if index == self.choice else ' '} {index + 1}. {option}")
			box = self.boxed(body)
			if self.cursor == "real":
				self.cursorAt = (len(rows) + 2 + len(question) + self.choice, 4)
			rows += box
			rows.append(MENU_HINT)
			return rows
		if self.busySince is not None:
			rows.append(self.statusRow())
			rows.append("")
		box = self.boxed([self.inputRow()])
		if self.cursor == "real":
			self.cursorAt = (len(rows) + 1, 2 + len(PROMPT) + self.caret)
		rows += box
		rows.append(BUSY_HINT if self.busySince is not None else IDLE_HINT)
		return rows

	def draw(self) -> None:
		rows = self.frame()
		if rows == self.previousRows and self.cursorAt == self.previousCursor and not self.finished:
			# Such a renderer writes nothing when its output has not changed.
			return
		out = HIDE_CURSOR if self.cursor == "real" else ""
		if self.cursor == "real" and self.previousCursor is not None:
			# Back to the row under the frame, where the next frame starts from.
			out += ESC + f"[{self.frameRows - self.previousCursor[0]}B" + "\r"
		finished, self.finished = self.finished, []
		if self.render == "full":
			erase = out + eraseLines(self.frameRows + 1 if self.frameRows else 0)
			frame = "\r\n".join(rows) + "\r\n"
			if finished:
				# Erase, write the finished text, write the live region: three writes, as such a
				# renderer makes them when it adds finished text, and one write otherwise.
				self.write(erase)
				self.write("".join(row + "\r\n" for row in finished))
				self.write(frame)
			else:
				self.write(erase + frame)
		else:
			out += ESC + f"[{self.frameRows}A" if self.frameRows else ""
			if finished:
				out += ESC + "[J" + "".join(row + "\r\n" for row in finished)
				out += "".join(ERASE_LINE + row + "\r\n" for row in rows)
			else:
				for index, row in enumerate(rows):
					if index < len(self.previousRows) and self.previousRows[index] == row:
						out += ESC + "[1B"
					else:
						out += ERASE_LINE + row + "\r\n"
				if len(rows) < len(self.previousRows):
					out += ESC + "[J"
			self.write(out)
		self.previousRows = rows
		self.frameRows = len(rows)
		self.previousCursor = self.cursorAt
		if self.cursorAt is not None:
			row, column = self.cursorAt
			self.write(ESC + f"[{self.frameRows - row}A" + ESC + f"[{column + 1}G" + SHOW_CURSOR)

	# The conversation

	def submit(self) -> None:
		text = self.typed
		self.typed, self.caret = "", 0
		if not text:
			return
		self.history.append(text)
		if text == "/exit":
			self.done = True
			return
		self.finished += [f"{PROMPT}{text}", ""]
		self.busySince = time.monotonic()
		self.tokens = 0
		self.script = list(REPLY)
		if text == "wait":
			self.script.insert(0, ("think", WAIT_SECONDS))
		self.advance()

	def advance(self) -> None:
		"""Start the next part of the conversation, or end it."""
		self.waitUntil = 0.0
		while self.script:
			part = self.script.pop(0)
			kind = part[0]
			if kind == "think":
				self.waitUntil = time.monotonic() + part[1]
				return
			if kind in ("say", "more"):
				self.streamPrefix = REPLY_MARK if kind == "say" else " "
				self.paragraph = part[1]
				words = part[1].split(" ")
				self.pieces = []
				while words:
					count = self.random.randint(*PIECE_WORDS)
					piece = " ".join(words[:count])
					words = words[count:]
					if len(piece) > 9 and self.random.random() < 0.5:
						# A piece can end inside a word, which wraps differently once it is whole.
						cut = self.random.randint(3, len(piece) - 3)
						self.pieces += [piece[:cut], piece[cut:]]
					else:
						self.pieces.append(piece)
					if words:
						self.pieces[-1] += " "
				self.streaming = ""
				self.nextPieceAt = time.monotonic()
				return
			if kind == "code":
				self.finished += [f"    {line}" for line in part[1]] + [""]
				continue
			if kind == "tool":
				self.running = part[1]
				self.output = part[2]
				self.waitUntil = time.monotonic() + 0.6
				return
			if kind == "ask":
				self.question = (part[1], part[2])
				self.choice = 0
				return
		self.busySince = None

	def tick(self) -> bool:
		"""Move the conversation on; whether anything changed."""
		if self.busySince is None:
			return False
		now = time.monotonic()
		if self.question is not None:
			return False
		if self.pieces:
			if now < self.nextPieceAt:
				return True
			if not self.streaming:
				self.paragraphStarted = True
			self.streaming += self.pieces.pop(0)
			self.tokens += self.random.randint(3, 9)
			self.nextPieceAt = now + self.random.uniform(*PIECE_SECONDS)
			if not self.pieces:
				self.finished += [*self.wrap(self.streaming, self.streamPrefix), ""]
				self.streaming = ""
				self.advance()
			return True
		if now < self.waitUntil:
			return True
		if self.running is not None:
			rows = self.wrap(self.running, REPLY_MARK)
			rows.append(f"  {OUTPUT_MARK}  {self.output[0]}")
			rows += [f"     {line}" for line in self.output[1:]]
			self.finished += [*rows, ""]
			self.running = None
		self.advance()
		return True

	def answer(self, key: str) -> None:
		_question, options = self.question
		if key == "up":
			self.choice = max(0, self.choice - 1)
		elif key == "down":
			self.choice = min(len(options) - 1, self.choice + 1)
		elif key in ("enter", "escape"):
			chosen = self.choice if key == "enter" else len(options) - 1
			self.question = None
			if chosen == len(options) - 1:
				# Declined: the tool does not run.
				self.script.pop(0)
				self.finished += [f"  {OUTPUT_MARK}  Declined", ""]
			self.advance()

	def edit(self, key: str) -> None:
		if key == "enter":
			self.submit()
		elif key == "backspace":
			if self.caret:
				self.typed = self.typed[: self.caret - 1] + self.typed[self.caret :]
				self.caret -= 1
		elif key == "delete":
			self.typed = self.typed[: self.caret] + self.typed[self.caret + 1 :]
		elif key == "left":
			self.caret = max(0, self.caret - 1)
		elif key == "right":
			self.caret = min(len(self.typed), self.caret + 1)
		elif key == "home":
			self.caret = 0
		elif key == "end":
			self.caret = len(self.typed)
		elif key in ("wordLeft", "wordRight"):
			step = -1 if key == "wordLeft" else 1
			position = self.caret
			while 0 < position + (step < 0) <= len(self.typed) and self.typed[position - (step < 0)] == " ":
				position += step
			while 0 < position + (step < 0) <= len(self.typed) and self.typed[position - (step < 0)] != " ":
				position += step
			self.caret = position
		elif key == "up" and self.history:
			self.typed = self.history[-1]
			self.caret = len(self.typed)
		elif key == "escape" and self.busySince is not None:
			self.script, self.pieces = [], []
			if self.streaming:
				self.finished += [*self.wrap(self.streaming, self.streamPrefix), ""]
				self.streaming = ""
			self.finished += ["  Interrupted by user", ""]
			self.running = None
			self.busySince = None
		elif key == "eof" and not self.typed:
			self.done = True
		elif len(key) == 1 and key.isprintable():
			self.typed = self.typed[: self.caret] + key + self.typed[self.caret :]
			self.caret += 1

	def run(self) -> None:
		self.previousRows: list[str] = []
		self.previousCursor = None
		if self.cursor == "drawn":
			self.write(HIDE_CURSOR)
		self.finished += ["✻ Assistant fixture", f"  {DIM}an imitation, for tests{NORMAL}", ""]
		self.draw()
		while not self.done:
			busy = self.busySince is not None
			ready, _, _ = select.select([sys.stdin.fileno()], [], [], 0.02 if busy else None)
			changed = False
			if ready:
				data = os.read(sys.stdin.fileno(), 1024).decode(errors="replace")
				for key in parseKeys(data):
					if self.question is not None:
						self.answer(key)
					else:
						self.edit(key)
					changed = True
			if self.tick():
				changed = True
			if changed and not self.done:
				self.draw()
				if self.paragraphStarted:
					self.paragraphStarted = False
					self.log("say", self.paragraph)
		# Leave the live region as finished text, as such a program does when it quits.
		if self.cursor == "real" and self.previousCursor is not None:
			self.write(ESC + f"[{self.frameRows - self.previousCursor[0]}B" + "\r")
		self.write(eraseLines(self.frameRows + 1) + SHOW_CURSOR)


KEYS = {
	"\r": "enter",
	"\n": "enter",
	"\x7f": "backspace",
	"\x08": "backspace",
	"\x04": "eof",
	"\x1b[A": "up",
	"\x1b[B": "down",
	"\x1b[C": "right",
	"\x1b[D": "left",
	"\x1b[H": "home",
	"\x1b[F": "end",
	"\x1bOH": "home",
	"\x1bOF": "end",
	"\x1b[1~": "home",
	"\x1b[4~": "end",
	"\x1b[3~": "delete",
	"\x1b[1;5C": "wordRight",
	"\x1b[1;5D": "wordLeft",
	"\x1b": "escape",
}


def parseKeys(data: str) -> list[str]:
	keys = []
	while data:
		for sequence in sorted(KEYS, key=len, reverse=True):
			if len(sequence) > 1 and data.startswith(sequence):
				keys.append(KEYS[sequence])
				data = data[len(sequence) :]
				break
		else:
			if data[:2] in (ESC + "[", ESC + "O"):
				# A sequence this does not know: skip to its final character.
				end = 2
				while end < len(data) and not data[end].isalpha() and data[end] != "~":
					end += 1
				data = data[end + 1 :]
			elif data[0] in KEYS:
				keys.append(KEYS[data[0]])
				data = data[1:]
			else:
				keys.append(data[0])
				data = data[1:]
	return keys


def main() -> None:
	options = dict(arg.removeprefix("--").split("=", 1) for arg in sys.argv[1:] if "=" in arg)
	assistant = Assistant(
		cursor=options.get("cursor", "drawn"),
		render=options.get("render", "full"),
		seed=int(options.get("seed", "1")),
		logPath=options.get("log", os.environ.get("BASEDREADER_ASSISTANT_LOG")),
	)
	saved = termios.tcgetattr(sys.stdin.fileno())
	tty.setraw(sys.stdin.fileno())
	try:
		assistant.run()
	finally:
		termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)


if __name__ == "__main__":
	main()
