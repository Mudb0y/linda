"""File operations whose Linux form differs from Windows'."""

import contextlib
import errno
import os


def replaceDurably(source: str, target: str) -> None:
	"""Put the already synced file at source in target's place, in one step that survives a crash.

	A crash at any point leaves either the old target or the new one, never a partial or missing
	file. The rename alone is atomic, but it is only durable once the directory that holds the new
	entry has been synced too.

	A symbolic link at target, such as one a dotfile manager placed, is followed, so it stays a link
	and the file it names is replaced. A link into a read-only or other file system makes the save
	fail, as a read-only file would, rather than quietly turning the link into a file.
	"""
	target = os.path.realpath(target)
	try:
		os.replace(source, target)
	except OSError:
		# Nothing else will ever remove the new content's file once it cannot take the target's name.
		with contextlib.suppress(OSError):
			os.remove(source)
		raise
	directory = os.open(os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY)
	try:
		os.fsync(directory)
	except OSError as e:
		# Some FUSE and network file systems cannot sync a directory. The rename has happened by now,
		# so the save has taken effect as durably as such a file system allows.
		if e.errno not in (errno.EINVAL, errno.ENOTSUP):
			raise
	finally:
		os.close(directory)
