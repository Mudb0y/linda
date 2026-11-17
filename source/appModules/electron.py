"""App module for a program run by a shared Electron whose own name cannot be found, so that it goes
by Electron's: its windows are web pages, read in browse mode as Chromium's are. On Windows, where
electron.exe would load it, it is the default app module."""

from .chromium import AppModule  # noqa: F401
