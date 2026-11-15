"""Stand-in for pycaw, Windows Core Audio through COM.

Its enumerations and callback base classes exist so that NVDA's audio modules import; anything
that would reach an audio device or session raises.
"""
