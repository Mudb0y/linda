from enum import IntEnum, IntFlag


class DEVICE_STATE(IntFlag):
	ACTIVE = 0x00000001
	DISABLED = 0x00000002
	NOTPRESENT = 0x00000004
	UNPLUGGED = 0x00000008
	MASK_ALL = 0x0000000F


class EDataFlow(IntEnum):
	eRender = 0
	eCapture = 1
	eAll = 2
	EDataFlow_enum_count = 3
