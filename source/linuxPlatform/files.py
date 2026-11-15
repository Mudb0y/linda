"""File operations whose Linux form differs from Windows'."""

import os


def replaceDurably(source: str, target: str) -> None:
	"""Put the already synced file at source in target's place, in one step that survives a crash.

	A crash at any point leaves either the old target or the new one, never a partial or missing
	file. The rename alone is atomic, but it is only durable once the directory that holds the new
	entry has been synced too.
	"""
	os.replace(source, target)
	directory = os.open(os.path.dirname(os.path.abspath(target)), os.O_RDONLY | os.O_DIRECTORY)
	try:
		os.fsync(directory)
	finally:
		os.close(directory)
