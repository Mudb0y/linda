# -*- coding: UTF-8 -*-
# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2018-2023 NV Access Limited, Cyrille Bougot
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING

import addonAPIVersion

if TYPE_CHECKING:
	from addonStore.models.version import SupportsVersionCheck

WINDOWS = "windows"
LINUX = "linux"
KNOWN_PLATFORMS: tuple[str, ...] = (WINDOWS, LINUX)
"""The platforms NVDA runs on, by the names an add-on's manifest gives them in its platforms list.
A manifest may name others, which are kept, so that an add-on can name a platform NVDA comes to run on
later without breaking older copies of NVDA."""
CURRENT_PLATFORM: str = WINDOWS if sys.platform == "win32" else LINUX
"""The platform this copy of NVDA runs on."""
_PLATFORM_DISPLAY_NAMES = {WINDOWS: "Windows", LINUX: "Linux"}


def getPlatformsFromManifestValue(value: str | Iterable[str] | None) -> tuple[str, ...]:
	"""The platforms a manifest's platforms list names, in lower case and without repeats.
	A manifest that names none was written for NVDA on Windows, as every add-on was before the list.
	"""
	if isinstance(value, str):
		value = value.split(",")
	names = (name.strip().lower() for name in value or ())
	return tuple(dict.fromkeys(name for name in names if name)) or (WINDOWS,)


def getPlatformDisplayName(platform: str) -> str:
	return _PLATFORM_DISPLAY_NAMES.get(platform, platform)


def supportsPlatform(addon: "SupportsVersionCheck", platform: str = CURRENT_PLATFORM) -> bool:
	"""True if the add-on says it supports the platform, by default the one this copy runs on."""
	return platform in addon.platforms


def hasAddonGotRequiredSupport(
	addon: "SupportsVersionCheck",
	currentAPIVersion: addonAPIVersion.AddonApiVersionT = addonAPIVersion.CURRENT,
) -> bool:
	"""True if NVDA provides the add-on with an API version high enough to meet the add-on's minimum requirements"""
	minVersion = addon.minimumNVDAVersion
	return minVersion <= currentAPIVersion


def isAddonTested(
	addon: "SupportsVersionCheck",
	backwardsCompatToVersion: addonAPIVersion.AddonApiVersionT = addonAPIVersion.BACK_COMPAT_TO,
) -> bool:
	"""True if this add-on is tested for the given API version.
	By default, the current version of NVDA is evaluated.
	"""
	return addon.lastTestedNVDAVersion >= backwardsCompatToVersion


def isAddonCompatible(
	addon: "SupportsVersionCheck",
	currentAPIVersion: addonAPIVersion.AddonApiVersionT = addonAPIVersion.CURRENT,
	backwardsCompatToVersion: addonAPIVersion.AddonApiVersionT = addonAPIVersion.BACK_COMPAT_TO,
) -> bool:
	"""Tests if the addon is compatible.
	The compatibility is defined by having the required features in NVDA, and by having been tested / built against
	an API version that is still supported by this version of NVDA.
	"""
	return hasAddonGotRequiredSupport(addon, currentAPIVersion) and isAddonTested(
		addon,
		backwardsCompatToVersion,
	)
