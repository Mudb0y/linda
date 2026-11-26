"""Controls for the Sound Themes settings panel on Linux."""

from __future__ import annotations

import os
import wave

import config
import ui
import wx
from gui import guiHelper, nvdaControls
from logHandler import log

from . import (
	DEFAULT_THEME,
	OVERRIDE_FILE_PREFIX,
	OVERRIDE_OFF,
	OVERRIDE_THEME,
	OVERRIDE_THEME_PREFIX,
	SOUND_EVENTS,
	SoundTheme,
	activeThemeId,
	availableThemes,
	playDirect,
	resolveSound,
	soundOverride,
)


class SoundThemesControls:
	"""Builds and manages the controls inside ``SoundThemesPanel``."""

	def __init__(self, panel: wx.Window, settingsSizer: wx.BoxSizer) -> None:
		self._panel = panel
		self._themes: list[SoundTheme] = availableThemes()
		self._themesById: dict[str, SoundTheme] = {theme.id: theme for theme in self._themes}
		self._events = SOUND_EVENTS
		currentTheme = activeThemeId()
		self._pendingTheme: str = currentTheme if currentTheme in self._themesById else DEFAULT_THEME
		self._pendingOverrides: dict[str, str] = {event.id: soundOverride(event.id) for event in self._events}
		self._customPaths: dict[str, str] = {}
		for event in self._events:
			override = self._pendingOverrides[event.id]
			if override.startswith(OVERRIDE_FILE_PREFIX):
				self._customPaths[event.id] = override[len(OVERRIDE_FILE_PREFIX) :]
		self._selectedIndex: int = 0
		self._sourceValues: list[str] = [
			OVERRIDE_THEME,
			OVERRIDE_OFF,
			*(f"{OVERRIDE_THEME_PREFIX}{theme.id}" for theme in self._themes),
			OVERRIDE_FILE_PREFIX,
		]

		sHelper = guiHelper.BoxSizerHelper(panel, sizer=settingsSizer)

		self.themeChoice: wx.Choice = sHelper.addLabeledControl(
			# Translators: Label for the sound theme selector in Sound Themes settings.
			_("Sound &theme:"),
			wx.Choice,
			choices=[theme.name for theme in self._themes],
		)
		self.themeChoice.SetSelection(self._themeIndex(self._pendingTheme))
		self.themeChoice.Bind(wx.EVT_CHOICE, self._onThemeChange)

		self.soundsList: nvdaControls.AutoWidthColumnListCtrl = sHelper.addLabeledControl(
			# Translators: Label for the list of sound events in Sound Themes settings.
			_("&Sounds:"),
			nvdaControls.AutoWidthColumnListCtrl,
			autoSizeColumn="LAST",
			style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
			size=(panel.scaleSize(420), panel.scaleSize(180)),
		)
		# Translators: Column header for the sound event name in Sound Themes settings.
		self.soundsList.AppendColumn(_("Sound"), width=panel.scaleSize(220))
		# Translators: Column header for the sound's current source in Sound Themes settings.
		self.soundsList.AppendColumn(_("Source"))
		for index, event in enumerate(self._events):
			self.soundsList.InsertItem(index, event.label)
			self.soundsList.SetItem(index, 1, self._sourceSummary(event.id))
		self.soundsList.Select(0)
		self.soundsList.Focus(0)
		self.soundsList.Bind(wx.EVT_LIST_ITEM_SELECTED, self._onSoundSelected)
		self.soundsList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._onSoundSelected)

		# Translators: Label for the group of controls editing the selected sound in Sound Themes settings.
		selectedSizer = wx.StaticBoxSizer(wx.VERTICAL, panel, label=_("Selected sound"))
		selectedGroup = guiHelper.BoxSizerHelper(panel, sizer=selectedSizer)
		sHelper.addItem(selectedGroup)

		sourceChoices = [
			# Translators: Option in Sound Themes settings meaning a sound follows the active theme.
			_("Theme default"),
			# Translators: Option in Sound Themes settings meaning a sound is muted/disabled.
			_("Off"),
			*(theme.name for theme in self._themes),
			# Translators: Option in Sound Themes settings meaning a sound uses a custom wave file.
			_("Custom file"),
		]
		self.sourceChoice: wx.Choice = selectedGroup.addLabeledControl(
			# Translators: Label for choosing the source of the selected sound in Sound Themes settings.
			_("Sound so&urce:"),
			wx.Choice,
			choices=sourceChoices,
		)
		self.sourceChoice.Bind(wx.EVT_CHOICE, self._onSourceChange)

		self.customFileEdit: wx.TextCtrl = selectedGroup.addLabeledControl(
			# Translators: Label for the read-only custom wave file path in Sound Themes settings.
			_("Custom &file:"),
			wx.TextCtrl,
			style=wx.TE_READONLY,
			size=(panel.scaleSize(300), -1),
		)

		actionButtons = guiHelper.ButtonHelper(wx.HORIZONTAL)
		self.browseButton: wx.Button = actionButtons.addButton(
			panel,
			# Translators: Button in Sound Themes settings to choose a custom .wav file.
			label=_("&Browse..."),
		)
		self.browseButton.Bind(wx.EVT_BUTTON, self._onBrowse)

		self.previewButton: wx.Button = actionButtons.addButton(
			panel,
			# Translators: Button in Sound Themes settings to preview the selected sound.
			label=_("&Preview"),
		)
		self.previewButton.Bind(wx.EVT_BUTTON, self._onPreview)
		selectedGroup.addItem(actionButtons)

		self.resetButton: wx.Button = sHelper.addItem(
			wx.Button(
				panel,
				# Translators: Button in Sound Themes settings to clear all per-sound overrides.
				label=_("&Reset sounds to theme defaults"),
			),
		)
		self.resetButton.Bind(wx.EVT_BUTTON, self._onReset)

		self._syncSelectedControls()

	def _isPanelGone(self) -> bool:
		return not self._panel or self._panel.IsBeingDeleted()

	def _themeIndex(self, themeId: str) -> int:
		for index, theme in enumerate(self._themes):
			if theme.id == themeId:
				return index
		return 0

	def _sourceSummary(self, soundId: str) -> str:
		override = self._pendingOverrides[soundId]
		if override == OVERRIDE_OFF:
			return _("Off")
		if override.startswith(OVERRIDE_FILE_PREFIX):
			path = override[len(OVERRIDE_FILE_PREFIX) :]
			if path:
				# Translators: Summary shown in the Sounds list when a custom .wav file is chosen.
				return _("Custom ({fileName})").format(fileName=os.path.basename(path))
			return _("Custom file")
		if override.startswith(OVERRIDE_THEME_PREFIX):
			targetId = override[len(OVERRIDE_THEME_PREFIX) :]
			theme = self._themesById.get(targetId)
			return theme.name if theme is not None else targetId
		return _("Theme default")

	def _sourceChoiceIndex(self, override: str) -> int:
		if override.startswith(OVERRIDE_FILE_PREFIX):
			return self._sourceValues.index(OVERRIDE_FILE_PREFIX)
		try:
			return self._sourceValues.index(override)
		except ValueError:
			return 0

	def _selectedSoundId(self) -> str:
		return self._events[self._selectedIndex].id

	def _syncSelectedControls(self) -> None:
		soundId = self._selectedSoundId()
		override = self._pendingOverrides[soundId]
		self.sourceChoice.SetSelection(self._sourceChoiceIndex(override))
		isCustom = override.startswith(OVERRIDE_FILE_PREFIX)
		customPath = override[len(OVERRIDE_FILE_PREFIX) :] if isCustom else ""
		self.customFileEdit.SetValue(customPath)
		self.customFileEdit.Enable(isCustom)

	def _refreshRow(self, index: int) -> None:
		event = self._events[index]
		self.soundsList.SetItem(index, 1, self._sourceSummary(event.id))

	def _onThemeChange(self, evt: wx.CommandEvent) -> None:
		if self._isPanelGone():
			return
		selection = self.themeChoice.GetSelection()
		if 0 <= selection < len(self._themes):
			self._pendingTheme = self._themes[selection].id

	def _onSoundSelected(self, evt: wx.ListEvent) -> None:
		evt.Skip()
		if self._isPanelGone():
			return
		index = evt.GetIndex()
		if 0 <= index < len(self._events):
			self._selectedIndex = index
			self._syncSelectedControls()

	def _onSourceChange(self, evt: wx.CommandEvent) -> None:
		if self._isPanelGone():
			return
		selection = self.sourceChoice.GetSelection()
		if not 0 <= selection < len(self._sourceValues):
			return
		soundId = self._selectedSoundId()
		value = self._sourceValues[selection]
		if value == OVERRIDE_FILE_PREFIX:
			remembered = self._customPaths.get(soundId, "")
			self._pendingOverrides[soundId] = f"{OVERRIDE_FILE_PREFIX}{remembered}"
		else:
			self._pendingOverrides[soundId] = value
		self._refreshRow(self._selectedIndex)
		self._syncSelectedControls()

	def _onBrowse(self, evt: wx.CommandEvent) -> None:
		if self._isPanelGone():
			return
		soundId = self._selectedSoundId()
		currentPath = self._customPaths.get(soundId, "")
		defaultDir = os.path.dirname(currentPath) if currentPath else ""
		defaultFile = os.path.basename(currentPath) if currentPath else ""
		with wx.FileDialog(
			self._panel,
			# Translators: Title of the file dialog to pick a custom wave sound.
			message=_("Choose sound file"),
			defaultDir=defaultDir,
			defaultFile=defaultFile,
			# Translators: File filter for wave sound files.
			wildcard=_("Wave files (*.wav)|*.wav"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		) as dialog:
			if dialog.ShowModal() != wx.ID_OK:
				return
			chosen = dialog.GetPath()
		try:
			with wave.open(chosen, "rb"):
				pass
		except Exception:  # noqa: BLE001
			log.debugWarning(f"Selected file is not a readable wave file: {chosen!r}", exc_info=True)
			self._panel._validationErrorMessageBox(
				# Translators: Error shown when a chosen custom sound file is not a valid WAV file.
				message=_("The selected file is not a readable wave (.wav) file."),
				option=_("Custom file"),
			)
			return
		self._customPaths[soundId] = chosen
		self._pendingOverrides[soundId] = f"{OVERRIDE_FILE_PREFIX}{chosen}"
		self._refreshRow(self._selectedIndex)
		self._syncSelectedControls()

	def _onPreview(self, evt: wx.CommandEvent) -> None:
		if self._isPanelGone():
			return
		soundId = self._selectedSoundId()
		override = self._pendingOverrides[soundId]
		if override == OVERRIDE_OFF:
			# Translators: Announced when previewing a sound that is turned off.
			ui.message(_("Sound is off"))
			return
		if override.startswith(OVERRIDE_FILE_PREFIX):
			customPath = override[len(OVERRIDE_FILE_PREFIX) :]
			if not customPath or not os.path.isfile(customPath):
				# Translators: Announced when previewing a custom sound before a file is chosen.
				ui.message(_("No custom sound file selected"))
				return
		resolved = resolveSound(
			soundId,
			themeId=self._pendingTheme,
			override=override,
			themesById=self._themesById,
		)
		if resolved is None:
			ui.message(_("Sound is off"))
			return
		try:
			playDirect(resolved, asynchronous=True)
		except Exception:  # noqa: BLE001
			log.debugWarning(f"Could not preview sound {resolved!r}", exc_info=True)
			# Translators: Announced when previewing a sound fails.
			ui.message(_("Could not play sound"))

	def _onReset(self, evt: wx.CommandEvent) -> None:
		if self._isPanelGone():
			return
		for index, event in enumerate(self._events):
			self._pendingOverrides[event.id] = OVERRIDE_THEME
			self._refreshRow(index)
		self._syncSelectedControls()
		# Translators: Announced when all per-sound overrides are reset to theme defaults.
		ui.message(_("All sounds reset to theme defaults"))

	def isValid(self) -> bool:
		for index, event in enumerate(self._events):
			override = self._pendingOverrides[event.id]
			if not override.startswith(OVERRIDE_FILE_PREFIX):
				continue
			customPath = override[len(OVERRIDE_FILE_PREFIX) :]
			if not customPath or not os.path.isfile(customPath):
				self._selectedIndex = index
				self.soundsList.Select(index)
				self.soundsList.Focus(index)
				self._syncSelectedControls()
				self._panel._validationErrorMessageBox(
					# Translators: Validation error when a sound is set to Custom file without a valid file.
					message=_("Please choose an existing wave (.wav) file for {sound}.").format(
						sound=event.label,
					),
					option=_("Custom file"),
				)
				return False
		return True

	def save(self) -> None:
		config.conf["soundThemes"]["theme"] = self._pendingTheme
		soundsConf = config.conf["soundThemes"]["sounds"]
		for event in self._events:
			override = self._pendingOverrides[event.id]
			if override == OVERRIDE_THEME:
				if event.id in soundsConf and soundsConf[event.id] != OVERRIDE_THEME:
					soundsConf[event.id] = OVERRIDE_THEME
			else:
				soundsConf[event.id] = override
