"""Stand-in for comInterfaces.IAccessible2Lib.

On Windows NVDA's build generates this module from IAccessible2's type library. Linux has neither
the library nor the interface, so the module exists, for modules that import it whole, and is
empty: any name read from it is an AttributeError, and any `from` import of a name an ImportError.
"""
