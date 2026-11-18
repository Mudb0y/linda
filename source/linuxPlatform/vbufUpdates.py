"""The thread that applies the engine's update batches to NVDA's buffer storage.

The engine watches every document it rendered and hands out a batch whenever one changes
(linuxPlatform.vbufEngine.next). One thread waits for those batches and queues each on NVDA's event
queue, where the main thread applies it to its buffer's storage, through the buffer's applyUpdate or
linuxPlatform.vbufLib.update, and then calls the buffer's onEngineEvents, which so reads the storage
as already changed. Nothing waits on the browser there: the batch is already rendered. The rest of a
large page replaces the whole document, which costs as much as building it, so this thread builds it
first and the main thread only swaps it in, leaving the old fields to this thread to free. On
Windows the backend applied updates inside the browser and told NVDA through vbufChangeNotify.
"""

import atexit
import threading
import time
from typing import TYPE_CHECKING

import queueHandler
from logHandler import log

from linuxPlatform import vbufEngine, vbufLib

if TYPE_CHECKING:
	from virtualBuffers.atspi import AtSpiBuffer

#: Milliseconds each wait for a batch may take, which bounds how long the thread takes to stop.
WAIT_MS = 500
#: Seconds the rest of a large page, and the end of a document's load, are kept for a buffer not yet
#: registered. Either can come before the thread that rendered the document has built the buffer
#: and registered it; the rest replaces the whole document, so nothing older than it is lost by
#: applying it late, and the end of the load is what the buffer waits for before reading a page
#: that was still loading. Other batches for a document no buffer has are dropped: the buffer's own
#: render is newer than they are.
HOLD_SECONDS = 10.0
#: The events that make a batch worth keeping for a buffer not yet registered.
_HELD_EVENTS = ("complete", "loadComplete")

_buffers: dict[tuple[str, str], "AtSpiBuffer"] = {}
#: The rest of each large page that came before its buffer registered, with when it came.
_held: dict[tuple[str, str], list[tuple[float, object]]] = {}
#: Batches whose buffer registered after they came, for the thread to apply before it waits again.
_ready: list[object] = []
#: Storage handles holding a document's old fields, swapped out on the main thread, to free here.
_spent: list[int] = []
#: Held while a batch is applied, so that once unregister returns no update touches that buffer.
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_atexitRegistered = False


def _engineNext():
	"""The engine's next, or None for an engine that cannot watch documents."""
	nextBatch = getattr(vbufEngine, "next", None)
	return nextBatch if callable(nextBatch) else None


def register(buffer: "AtSpiBuffer") -> None:
	"""Apply the engine's updates for buffer's document to its storage from now on.

	The buffer needs busName, docPath and VBufHandle, and is told of each batch through
	onEngineEvents(events, info) on the main thread, after the batch is in its storage.
	"""
	key = (buffer.busName, buffer.docPath)
	with _lock:
		_buffers[key] = buffer
		_ready.extend(batch for _came, batch in _held.pop(key, ()))
	_start()


def unregister(buffer: "AtSpiBuffer") -> None:
	"""Stop applying updates to buffer. Call it before the buffer's storage is destroyed: once it
	returns, no update is being applied to that storage, and none will be."""
	with _lock:
		key = (buffer.busName, buffer.docPath)
		if _buffers.get(key) is buffer:
			del _buffers[key]
		for _came, batch in _held.pop(key, ()):
			if batch.stream is not None:
				_release(batch.stream)


def _start() -> None:
	global _thread, _atexitRegistered
	nextBatch = _engineNext()
	if nextBatch is None:
		return
	with _lock:
		if _thread is not None and _thread.is_alive():
			return
		_stop.clear()
		_thread = threading.Thread(
			name=f"{__name__}.updates",
			target=_run,
			args=(nextBatch,),
			daemon=True,
		)
		_thread.start()
		if not _atexitRegistered:
			atexit.register(terminate)
			_atexitRegistered = True


def terminate() -> None:
	"""Stop the thread and the engine's watching, as NVDA exits."""
	global _thread
	_stop.set()
	shutdown = getattr(vbufEngine, "shutdown", None)
	if callable(shutdown):
		try:
			shutdown()
		except Exception:
			log.exception("The engine did not shut down")
	thread = _thread
	if thread is not None and thread is not threading.current_thread():
		thread.join(timeout=WAIT_MS / 1000 * 4)
		if thread.is_alive():
			log.debugWarning("The update thread is still waiting on the engine")
	_thread = None
	_freeSpent()


def _release(stream, *, onMainThread: bool = False) -> None:
	"""Free stream and the storage built from it, which holds the old fields once update took the
	new. On the main thread that storage is left to the update thread, as freeing a whole document
	takes as long as building one."""
	replacement = getattr(stream, "replacement", None)
	stream.replacement = None
	stream.free()
	if replacement is None:
		return
	if onMainThread:
		with _lock:
			if _thread is not None and _thread.is_alive():
				_spent.append(replacement[0])
				return
	vbufLib.destroy(replacement[0])


def _freeSpent() -> None:
	with _lock:
		spent = _spent[:]
		_spent.clear()
	for handle in spent:
		vbufLib.destroy(handle)


def _run(nextBatch) -> None:
	while not _stop.is_set():
		_freeSpent()
		with _lock:
			ready = _ready[:]
			_ready.clear()
		for batch in ready:
			try:
				_apply(batch, hold=False)
			except Exception:
				log.exception("An update from the engine could not be applied")
		try:
			batch = nextBatch(WAIT_MS)
		except Exception:
			log.exception("The engine's updates stopped")
			return
		if batch is None:
			continue
		try:
			_apply(batch)
		except Exception:
			log.exception("An update from the engine could not be applied")


def _dropExpiredHeld(now: float) -> None:
	"""Free the batches held longer than HOLD_SECONDS. Call it holding _lock."""
	for key in list(_held):
		kept = []
		for came, batch in _held[key]:
			if now - came <= HOLD_SECONDS:
				kept.append((came, batch))
			elif batch.stream is not None:
				_release(batch.stream)
		if kept:
			_held[key] = kept
		else:
			del _held[key]


def _apply(batch, *, hold: bool = True) -> None:
	kept = False
	try:
		with _lock:
			key = (batch.busName, batch.docPath)
			buffer = _buffers.get(key)
			if buffer is None:
				now = time.monotonic()
				_dropExpiredHeld(now)
				if hold and any(event in batch.events for event in _HELD_EVENTS):
					_held.setdefault(key, []).append((now, batch))
					kept = True
				return
		if batch.stream is not None and "complete" in batch.events:
			# The rest of a large page, which replaces the whole document: built here, not on the main thread.
			batch.stream.replacement = vbufLib.buildReplacement(batch.stream)
		log.debug(f"Update for {batch.busName} {batch.docPath}: {batch.events} {batch.info}")
		queueHandler.queueFunction(queueHandler.eventQueue, _applyToBuffer, buffer, batch)
		kept = True
	finally:
		if batch.stream is not None and not kept:
			_release(batch.stream)


def _applyToBuffer(buffer: "AtSpiBuffer", batch) -> None:
	"""On the main thread, in the order the batches came: apply batch to buffer's storage, then tell
	the buffer. NVDA keeps positions in a buffer outside its storage, say all's among them, and only
	the main thread can keep them on their text while the storage changes under them."""
	try:
		with _lock:
			registered = _buffers.get((batch.busName, batch.docPath)) is buffer
		if not registered:
			return
		if batch.stream is not None and buffer.VBufHandle:
			applyUpdate = getattr(buffer, "applyUpdate", None)
			if applyUpdate is not None:
				applied = applyUpdate(batch.stream)
			else:
				applied = vbufLib.update(buffer.VBufHandle, batch.stream)
			if not applied:
				# The buffer stays as it was, or lacks the fields it no longer had; the next change to
				# the document renders them again.
				log.debugWarning(
					f"Update for {batch.busName} {batch.docPath} not wholly applied: {batch.info}",
				)
	finally:
		if batch.stream is not None:
			_release(batch.stream, onMainThread=True)
	onEngineEvents = getattr(buffer, "onEngineEvents", None)
	if onEngineEvents is not None:
		onEngineEvents(batch.events, batch.info)
