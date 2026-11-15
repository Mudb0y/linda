"""Linux replacement for UIAHandler, skeleton.

UI Automation is a Windows API, so on Linux NVDA never has a UI Automation client. handler stays
None, which every caller already treats as UI Automation being unavailable, as it is on Windows
when UI Automation is turned off. Its submodules are Windows only and are not provided.
"""

#: The UI Automation client. There is none on Linux.
handler = None


def initialize() -> None:
	pass


def terminate() -> None:
	pass
