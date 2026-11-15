"""Stand-in for comInterfaces.UIAutomationClient.

On Windows NVDA's build generates this module from UI Automation's type library. Linux has neither
the library nor UI Automation, so the module exists, for modules that import it whole, and is
empty: any name read from it is an AttributeError, and any `from` import of a name an ImportError.
"""
