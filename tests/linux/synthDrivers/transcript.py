"""A synth driver for tests: it records what the speech manager sends and reports indexes.

The records go to synth.events and, when LINDA_TRANSCRIPT_FILE names a file, to that file as
JSON Lines; _transcriptRecords describes the format and reads it back. LINDA_TRANSCRIPT_MODE
chooses when indexes are reported:
- immediate, the default: every index and then done, inside speak().
- manual: only when the test calls advance(); done counts as one step. Nothing is released while
paused, as a real driver's player holds its callbacks.
- timed: from a worker thread, each index after LINDA_TRANSCRIPT_MS_PER_CHAR milliseconds for
every character spoken before it, not counting time spent paused.
- player: as eSpeak's driver does it, through an nvwave.WavePlayer. A worker thread feeds
LINDA_TRANSCRIPT_MS_PER_CHAR milliseconds of audio for every character, with each index as
the completion callback of the audio before it, idles the player at the end of each utterance and
then reports done. cancel() stops the player and pause() pauses it, so whatever the player does
with its callbacks is what the speech manager sees.

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
from speech.types import SpeechSequence
from synthDriverHandler import SynthDriver as BaseSynthDriver
from synthDriverHandler import VoiceInfo, synthDoneSpeaking, synthIndexReached

modes = ("immediate", "manual", "timed", "player")

#: The rate player mode feeds at, eSpeak's.
PLAYER_RATE = 22050
#: Every frame player mode feeds, mono 16-bit. Not silence, which a speech player trims.
PLAYER_FRAME = (1000).to_bytes(2, "little", signed=True)


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

	def __init__(self) -> None:
		super().__init__()
		self.mode = os.environ.get("LINDA_TRANSCRIPT_MODE") or "immediate"
		if self.mode not in modes:
			raise ValueError(f"LINDA_TRANSCRIPT_MODE is {self.mode!r}, not one of {modes}")
		self.msPerChar = float(os.environ.get("LINDA_TRANSCRIPT_MS_PER_CHAR") or 10)
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
		self._path = os.environ.get("LINDA_TRANSCRIPT_FILE") or None
		self._worker = None
		self._player = None
		if self.mode == "player":
			import config
			import nvwave

			self._player = nvwave.WavePlayer(
				channels=1,
				samplesPerSec=PLAYER_RATE,
				bitsPerSample=16,
				outputDevice=config.conf["audio"]["outputDevice"],
			)
		run = {"timed": self._runTimed, "player": self._runPlayer}.get(self.mode)
		if run:
			self._worker = threading.Thread(target=run, name="transcriptSynth", daemon=True)
			self._worker.start()

	def _getAvailableVoices(self) -> OrderedDict[str, VoiceInfo]:
		return OrderedDict(transcript=VoiceInfo("transcript", "Transcript", "en"))

	def _get_voice(self) -> str:
		return self._settings["voice"]

	def _set_voice(self, value: str) -> None:
		if value not in self.availableVoices:
			raise LookupError(value)
		self._settings["voice"] = value

	def _get_rate(self) -> int:
		return self._settings["rate"]

	def _set_rate(self, value: int) -> None:
		self._settings["rate"] = value

	def _get_pitch(self) -> int:
		return self._settings["pitch"]

	def _set_pitch(self, value: int) -> None:
		self._settings["pitch"] = value

	def _get_volume(self) -> int:
		return self._settings["volume"]

	def _set_volume(self, value: int) -> None:
		self._settings["volume"] = value

	def _record(self, kind: str, data: Any = None) -> None:
		# Callers hold the lock, so that the order of records is the order of the state changes.
		record = {"t": time.monotonic(), "kind": kind, "data": data}
		self.events.append(record)
		if self._path:
			# Opened for each record, so that every record is on disk even if the process is killed.
			with open(self._path, "a", encoding="utf-8") as f:
				f.write(json.dumps(record) + "\n")

	def speak(self, speechSequence: SpeechSequence) -> None:
		marks: list[tuple[int | None, int]] = []
		chars = 0
		for item in speechSequence:
			if isinstance(item, str):
				chars += len(item)
			elif isinstance(item, IndexCommand):
				marks.append((item.index, chars))
				chars = 0
		marks.append((None, chars))
		with self._condition:
			self._record("speak", [render(item) for item in speechSequence])
			if self.mode != "immediate":
				self._pending.extend(marks)
				self._condition.notify_all()
				return
		for index, _chars in marks:
			self._notify(index)

	def _notify(self, index: int | None) -> None:
		if index is None:
			synthDoneSpeaking.notify(synth=self)
		else:
			synthIndexReached.notify(synth=self, index=index)

	@property
	def pendingNotifications(self) -> list[int | None]:
		"""The indexes not yet reported, in order, with None standing for done.

		In player mode, those whose audio has not yet been handed to the player.
		"""
		with self._condition:
			return [index for index, _chars in self._pending]

	def advance(self, count: int = 1) -> int:
		"""Manual mode: report the next count notifications, and return how many there were.

		While paused nothing is reported.
		"""
		if self.mode != "manual":
			raise RuntimeError(f"advance() is for manual mode, and this synth is in {self.mode} mode")
		reported = 0
		with self._condition:
			while reported < count and self._pending and not self._paused:
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

	def _runPlayer(self) -> None:
		while True:
			with self._condition:
				while not self._pending and not self._terminated:
					self._condition.wait()
				if self._terminated:
					return
				generation = self._generation
				index, chars = self._pending.pop(0)
			audio = PLAYER_FRAME * round(chars * self.msPerChar * PLAYER_RATE / 1000)
			onDone = None if index is None else lambda index=index: self._notify(index)
			self._player.feed(audio, onDone=onDone)
			if generation != self._generation:
				# Cancelled between taking this piece and feeding it, so the cancel's stop came too
				# early to discard it.
				self._player.stop()
				continue
			if index is None:
				self._player.idle()
				with self._condition:
					if generation == self._generation and not self._terminated:
						self._notify(None)

	def cancel(self) -> None:
		with self._condition:
			self._pending.clear()
			self._generation += 1
			# NVDA's cancelSpeech ends a pause without calling pause(False), as stopping a real player does.
			self._paused = False
			self._condition.notify_all()
			self._record("cancel")
		if self._player:
			# Outside the lock, as eSpeak's stop() is: the worker may be blocked in feed.
			self._player.stop()

	def pause(self, switch: bool) -> None:
		with self._condition:
			self._record("pause", switch)
			self._paused = switch
			self._condition.notify_all()
		if self._player:
			self._player.pause(switch)

	def terminate(self) -> None:
		with self._condition:
			self._pending.clear()
			self._terminated = True
			self._condition.notify_all()
		if self._player:
			self._player.stop()
		if self._worker and self._worker is not threading.current_thread():
			self._worker.join()
		if self._player:
			self._player.close()
		super().terminate()
