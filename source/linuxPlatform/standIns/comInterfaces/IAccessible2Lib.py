"""Stand-in for comInterfaces.IAccessible2Lib.

On Windows NVDA's build generates this module from IAccessible2's type library. Linux has neither
the library nor the interface, so only the interfaces that NVDAObjects.IAccessible names at import
are here, with the IIDs from IAccessible2's IDL. Nothing can obtain them. Any other name read from
it is an AttributeError.
"""

from comInterfaces.Accessibility import IAccessible
from comtypes import GUID, IUnknown


class IAccessible2(IAccessible):
	_iid_ = GUID("{E89F726E-C4F4-4C19-BB19-B647D7FA8478}")


class IAccessibleRelation(IUnknown):
	_iid_ = GUID("{7CDF86EE-C3DA-496A-BDA4-281B336E1FDC}")
