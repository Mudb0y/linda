"""Stand-in for comInterfaces.tom.

On Windows NVDA's build generates this module from the Text Object Model type library in
msftedit.dll. Linux has neither, so the module exists and is empty: any name read from it is an
AttributeError, and any `from` import of a name an ImportError.
"""
