"""Reading the transcript synth's records, with no NVDA module needed.

The transcript driver writes one JSON object per line: {"t": monotonic seconds, "kind": "speak",
"cancel" or "pause", "data": ...}. A speak record's data is the sequence the speech manager sent,
strings verbatim and each command as a one-key object such as {"index": 3}.

The leading underscore keeps getSynthList from taking this module for a driver.
"""

import json
from collections.abc import Iterable
from pathlib import Path

#: Appended by NVDA to every string spoken outside character mode; speech.speech.CHUNK_SEPARATOR.
chunkSeparator = "  "


def readRecords(path: str | Path) -> list[dict]:
	with open(path, encoding="utf-8") as f:
		return [json.loads(line) for line in f if line.strip()]


def spokenText(record: dict) -> str:
	"""The strings of one speak record, without chunk separators, joined by single spaces."""
	pieces = (item.removesuffix(chunkSeparator).strip() for item in record["data"] if isinstance(item, str))
	return " ".join(piece for piece in pieces if piece)


def spokenTexts(records: Iterable[dict]) -> list[str]:
	return [spokenText(record) for record in records if record["kind"] == "speak"]
