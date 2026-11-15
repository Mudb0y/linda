"""A synth driver for tests: it records what the speech manager sends and reports indexes.

The records go to synth.events and, when TRANSCRIPT_SYNTH_FILE names a file, to that file as JSON
Lines; _transcriptRecords describes the format and reads it back. TRANSCRIPT_SYNTH_MODE chooses
when indexes are reported:
- immediate, the default: every index and then done, inside speak().
- manual: only when the test calls advance(); done counts as one step.
- timed: from a worker thread, each index after TRANSCRIPT_SYNTH_MS_PER_CHAR milliseconds for every
character spoken before it, not counting time spent paused.

Notifications go through synthIndexReached and synthDoneSpeaking as a real driver's do, so the
speech manager runs exactly the code it runs for one.
"""

import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any

from speech.commands import (
	BreakCommand,
	CharacterModeCommand,
	IndexCommand,
	LangChangeCommand,
	PhonemeCommand,
	PitchCommand,
	RateCommand,
	VolumeCommand,
)
from synthDriverHandler import SynthDriver as BaseSynthDriver
from synthDriverHandler import VoiceInfo, synthDoneSpeaking, synthIndexReached

modes = ("immediate", "manual", "timed")


def render(item: object) -> Any:
	if isinstance(item, str):
		return item
	if isinstance(item, IndexCommand):
		return {"index": item.index}
	if isinstance(item, LangChangeCommand):
		return {"lang": item.lang}
	if isinstance(item, CharacterModeCommand):
		return {"charMode": item.state}
	if isinstance(item, BreakCommand):
		return {"break": item.time}
	if isinstance(item, (PitchCommand, RateCommand, VolumeCommand)):
		# The value the synth ends up at, so that assertions hold whatever the configured default.
		return {item.settingName: item.newValue}
	if isinstance(item, PhonemeCommand):
		return {"ipa": item.ipa, "text": item.text}
	# Drivers must tolerate commands they do not know; recording one lets a test see it arrived.
	return {"unknown": repr(item)}


class SynthDriver(BaseSynthDriver):
	name = "transcript"
	description = "Transcript (testing)"
	supportedSettings = (
		BaseSynthDriver.VoiceSetting(),
		BaseSynthDriver.RateSetting(),
		BaseSynthDriver.PitchSetting(),
		BaseSynthDriver.VolumeSetting(),
	)
	supportedCommands = frozenset(
		{
			IndexCommand,
			CharacterModeCommand,
			LangChangeCommand,
			BreakCommand,
			PitchCommand,
			RateCommand,
			VolumeCommand,
			PhonemeCommand,
		},
	)
	supportedNotifications = frozenset({synthIndexReached, synthDoneSpeaking})

	@classmethod
	def check(cls) -> bool:
		return True

	def __init__(self):
		super().__init__()
		self.mode = os.environ.get("TRANSCRIPT_SYNTH_MODE") or "immediate"
		if self.mode not in modes:
			raise ValueError(f"TRANSCRIPT_SYNTH_MODE is {self.mode!r}, not one of {modes}")
		self.msPerChar = float(os.environ.get("TRANSCRIPT_SYNTH_MS_PER_CHAR") or 10)
		self.events: list[dict] = []
		self._settings = {setting.id: setting.defaultVal for setting in self.supportedSettings}
		self._settings["voice"] = "transcript"
		# Each entry is an index, or None for done, with the characters spoken since the last one.
		self._pending: list[tuple[int | None, int]] = []
		self._generation = 0
		self._paused = False
		self._terminated = False
		# Reentrant, so that a handler of a notification may call cancel() on the same thread.
		self._condition = threading.Condition(threading.RLock())
		self._path = os.environ.get("TRANSCRIPT_SYNTH_FILE") or None
		self._worker = None
		if self.mode == "timed":
			self._worker = threading.Thread(target=self._runTimed, name="transcriptSynth", daemon=True)
			self._worker.start()

	def _getAvailableVoices(self) -> OrderedDict[str, VoiceInfo]:
		return OrderedDict(transcript=VoiceInfo("transcript", "Transcript", "en"))

	def _get_voice(self) -> str:
		return self._settings["voice"]

	def _set_voice(self, value: str):
		if value not in self.availableVoices:
			raise LookupError(value)
		self._settings["voice"] = value

	def _get_rate(self) -> int:
		return self._settings["rate"]

	def _set_rate(self, value: int):
		self._settings["rate"] = value

	def _get_pitch(self) -> int:
		return self._settings["pitch"]

	def _set_pitch(self, value: int):
		self._settings["pitch"] = value

	def _get_volume(self) -> int:
		return self._settings["volume"]

	def _set_volume(self, value: int):
		self._settings["volume"] = value

	def _record(self, kind: str, data: Any = None) -> None:
		record = {"t": time.monotonic(), "kind": kind, "data": data}
		self.events.append(record)
		if self._path:
			# Opened for each record, so that every record is on disk even if the process is killed.
			with open(self._path, "a", encoding="utf-8") as f:
				f.write(json.dumps(record) + "\n")

	def speak(self, speechSequence: list) -> None:
		self._record("speak", [render(item) for item in speechSequence])
		marks: list[tuple[int | None, int]] = []
		chars = 0
		for item in speechSequence:
			if isinstance(item, str):
				chars += len(item)
			elif isinstance(item, IndexCommand):
				marks.append((item.index, chars))
				chars = 0
		marks.append((None, chars))
		if self.mode == "immediate":
			for index, _chars in marks:
				self._notify(index)
			return
		with self._condition:
			self._pending.extend(marks)
			self._condition.notify_all()

	def _notify(self, index: int | None) -> None:
		if index is None:
			synthDoneSpeaking.notify(synth=self)
		else:
			synthIndexReached.notify(synth=self, index=index)

	@property
	def pendingNotifications(self) -> list[int | None]:
		"""The indexes not yet reported, in order, with None standing for done."""
		with self._condition:
			return [index for index, _chars in self._pending]

	def advance(self, count: int = 1) -> int:
		"""Manual mode: report the next count notifications, and return how many there were."""
		if self.mode != "manual":
			raise RuntimeError(f"advance() is for manual mode, and this synth is in {self.mode} mode")
		reported = 0
		with self._condition:
			while reported < count and self._pending:
				index, _chars = self._pending.pop(0)
				# Under the lock, so that a cancel on another thread cannot be followed by a stale index.
				self._notify(index)
				reported += 1
		return reported

	def _runTimed(self) -> None:
		with self._condition:
			while not self._terminated:
				if not self._pending or self._paused:
					self._condition.wait()
					continue
				generation = self._generation
				index, chars = self._pending[0]
				remaining = chars * self.msPerChar / 1000
				while generation == self._generation and not self._terminated:
					if self._paused:
						self._condition.wait()
						continue
					if remaining <= 0:
						break
					start = time.monotonic()
					self._condition.wait(remaining)
					remaining -= time.monotonic() - start
				if generation != self._generation or self._terminated:
					continue
				self._pending.pop(0)
				self._notify(index)

	def cancel(self) -> None:
		with self._condition:
			self._pending.clear()
			self._generation += 1
			# NVDA's cancelSpeech ends a pause without calling pause(False), as stopping a real player does.
			self._paused = False
			self._condition.notify_all()
		self._record("cancel")

	def pause(self, switch: bool) -> None:
		self._record("pause", switch)
		with self._condition:
			self._paused = switch
			self._condition.notify_all()

	def terminate(self) -> None:
		with self._condition:
			self._pending.clear()
			self._terminated = True
			self._condition.notify_all()
		if self._worker and self._worker is not threading.current_thread():
			self._worker.join()
		super().terminate()
