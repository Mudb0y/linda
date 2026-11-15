"""Stand-in for comInterfaces.SpeechLib.

On Windows NVDA's build generates this module from SAPI 5's type library. Linux has neither, so
the module exists and is empty: any `from` import of a name from it is an ImportError.
"""
