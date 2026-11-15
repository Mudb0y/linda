"""Stand-in for comInterfaces.Accessibility.

On Windows NVDA's build generates this module from the type library in oleacc.dll. Linux has no
type library, so only the interface that modules on the import path name at import is here.
"""

from comtypes.automation import IDispatch
from comtypes import GUID


class IAccessible(IDispatch):
	_iid_ = GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")
