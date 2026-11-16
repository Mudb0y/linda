"""Sample-level work on the formats nvwave plays: silence, leading-silence detection and gain."""

import array
import sys
from collections.abc import Sequence

from . import WAVE_FORMAT_IEEE_FLOAT, WAVE_FORMAT_PCM, StreamFormat

# Integer limits, and the typecodes that unpack whole samples, for the integer widths that have one.
_intMax = {8: 255, 16: 32767, 24: 8388607, 32: 2147483647}
_intMin = {8: 0, 16: -32768, 24: -8388608, 32: -2147483648}
_intTypecodes = {8: "B", 16: "h", 32: "i"}
_floatTypecodes = {32: "f", 64: "d"}


def _zeroPoint(fmt: StreamFormat) -> int:
	# 8-bit PCM is unsigned, centred on 128; wider PCM is signed.
	return 128 if fmt.formatTag == WAVE_FORMAT_PCM and fmt.bitsPerSample == 8 else 0


def _samples(fmt: StreamFormat, data: bytes | memoryview) -> Sequence[int] | Sequence[float] | None:
	"""Every whole sample of data, in order, or None for a format this module does not know."""
	bits = fmt.bitsPerSample
	if fmt.formatTag == WAVE_FORMAT_PCM:
		typecode = _intTypecodes.get(bits)
	elif fmt.formatTag == WAVE_FORMAT_IEEE_FLOAT:
		typecode = _floatTypecodes.get(bits)
	else:
		return None
	isPcm24 = fmt.formatTag == WAVE_FORMAT_PCM and bits == 24
	if typecode is None and not isPcm24:
		return None
	bytesPerSample = bits // 8
	data = memoryview(data)[: len(data) - len(data) % bytesPerSample]
	if isPcm24:
		return [int.from_bytes(data[i : i + 3], "little", signed=True) for i in range(0, len(data), 3)]
	samples = array.array(typecode)
	samples.frombytes(data)
	if sys.byteorder != "little":
		samples.byteswap()
	return samples


def _toBytes(fmt: StreamFormat, samples: Sequence[int] | Sequence[float]) -> bytes:
	bits = fmt.bitsPerSample
	if fmt.formatTag == WAVE_FORMAT_PCM and bits == 24:
		return b"".join(int(s).to_bytes(3, "little", signed=True) for s in samples)
	typecode = (_intTypecodes if fmt.formatTag == WAVE_FORMAT_PCM else _floatTypecodes)[bits]
	out = array.array(typecode, samples)
	if sys.byteorder != "little":
		out.byteswap()
	return out.tobytes()


def silence(fmt: StreamFormat, frames: int) -> bytes:
	fill = _zeroPoint(fmt).to_bytes(1, "little")
	return fill * (frames * fmt.blockAlign)


def leadingSilenceSize(fmt: StreamFormat, data: bytes) -> int:
	"""How many bytes at the start of data are silence, in whole frames.

	The same answer as upstream's SilenceDetect::getLeadingSilenceSize: a sample is silent within
	1/1024 of full scale of the zero point, channels are not told apart, and a format it does not
	know has no leading silence.
	"""
	samples = _samples(fmt, data)
	if samples is None:
		return 0
	bytesPerSample = fmt.bitsPerSample // 8
	if len(data) < bytesPerSample:
		size = 0
	else:
		if fmt.formatTag == WAVE_FORMAT_IEEE_FLOAT:
			threshold: float = 1 / 1024
		else:
			# Integer division, as the C++ computes it: 8-bit audio's threshold is 0.
			threshold = _intMax[fmt.bitsPerSample] // 1024
		zero = _zeroPoint(fmt)
		low, high = zero - threshold, zero + threshold
		loud = next((i for i, sample in enumerate(samples) if sample < low or sample > high), None)
		size = len(data) if loud is None else loud * bytesPerSample
	return size - size % fmt.blockAlign


def applyGain(fmt: StreamFormat, data: bytes, volumes: Sequence[float]) -> bytes:
	"""data with each channel scaled by its volume, from 0 to 1."""
	if all(volume == 1.0 for volume in volumes):
		return data
	samples = _samples(fmt, data)
	if samples is None:
		raise ValueError(f"no gain for format {fmt}")
	channels = fmt.channels
	if fmt.formatTag == WAVE_FORMAT_IEEE_FLOAT:
		scaled: list = [sample * volumes[i % channels] for i, sample in enumerate(samples)]
	else:
		zero = _zeroPoint(fmt)
		low, high = _intMin[fmt.bitsPerSample], _intMax[fmt.bitsPerSample]
		scaled = [
			min(high, max(low, zero + round((sample - zero) * volumes[i % channels])))
			for i, sample in enumerate(samples)
		]
	return _toBytes(fmt, scaled)
