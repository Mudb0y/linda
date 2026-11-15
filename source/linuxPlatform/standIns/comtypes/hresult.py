"""Stand-in for comtypes.hresult: the HRESULT values NVDA names, signed as comtypes gives them."""


def _signed(value: int) -> int:
	return value - (1 << 32) if value & 0x80000000 else value


S_OK = 0
S_FALSE = 1
E_UNEXPECTED = _signed(0x8000FFFF)
E_NOTIMPL = _signed(0x80004001)
E_INVALIDARG = _signed(0x80070057)
E_OUTOFMEMORY = _signed(0x8007000E)
