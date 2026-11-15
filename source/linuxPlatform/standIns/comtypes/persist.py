"""Stand-in for comtypes.persist."""

from comtypes import GUID, IUnknown


class IPersist(IUnknown):
	_iid_ = GUID("{0000010C-0000-0000-C000-000000000046}")
