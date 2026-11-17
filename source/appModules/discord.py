"""App module for Discord, which runs its own Electron: its window is a web page, read in browse mode
as Chromium's are, as NVDA reads it on Windows. On Windows, where Discord.exe would load it, it is
the default app module."""

from .chromium import AppModule  # noqa: F401
