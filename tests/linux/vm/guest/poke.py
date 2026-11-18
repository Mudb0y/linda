"""What a test does to the key capture helper and the keyboards from outside, as root.

	poke.py grab NAME
	poke.py leds NAME
	poke.py stall PID SECONDS
	poke.py race back|across PID NAME KEY

grab holds an exclusive grab on the keyboard named NAME, as another key remapper would, until it is
killed; it prints "grabbed" once it holds it. leds prints, as a JSON object, the lock lights the
kernel has on for each input device named NAME, by event node, as bits of the LED codes. stall stops
the thread PID, and that thread alone, for SECONDS, through ptrace: the helper's loop is its main
thread, whose number is the process's, and its watchdog thread goes on.

race makes a uhid keyboard named NAME, as fakeKeyboard.py does, and follows the helper's loop through
ptrace from one system call to the next, so as to press KEY on that keyboard at the exact moment the
helper is about to grab it: as the loop enters EVIOCGRAB on it, after its look at the keys found all
of them up. With "back", KEY stays down, so that the helper's second look, after the grab, finds it
down; with "across", KEY is released as the loop enters that second look, so that it finds every key
up again, the race the helper accepts. It prints a JSON line for each step, lets the loop go on
untraced, and then serves the keyboard as fakeKeyboard.py serve does.
"""

import ctypes
import json
import os
import signal
import sys
import time

import fakeKeyboard
from evdev import InputDevice, ecodes, list_devices

PTRACE_GETREGS = 12
PTRACE_DETACH = 17
PTRACE_SYSCALL = 24
PTRACE_SEIZE = 0x4206
PTRACE_INTERRUPT = 0x4207
PTRACE_O_TRACESYSGOOD = 1
PTRACE_EVENT_STOP = 128
WALL = 0x40000000
SYS_IOCTL = 16
ENOSYS = 38
#: The helper's requests, as evdev.rs makes them: EVIOCGRAB, and EVIOCGKEY of 96 bytes.
EVIOCGRAB = 0x40044590
EVIOCGKEY = 0x80604518


class Registers(ctypes.Structure):
	"""struct user_regs_struct on x86_64."""

	_fields_ = [
		(name, ctypes.c_ulonglong)
		for name in (
			*("r15", "r14", "r13", "r12", "rbp", "rbx", "r11", "r10", "r9", "r8", "rax", "rcx", "rdx"),
			*("rsi", "rdi", "orig_rax", "rip", "cs", "eflags", "rsp", "ss", "fs_base", "gs_base"),
			*("ds", "es", "fs", "gs"),
		)
	]


def devices(name: str) -> list[InputDevice]:
	found = []
	for path in list_devices():
		device = InputDevice(path)
		if device.name == name:
			found.append(device)
		else:
			device.close()
	if not found:
		sys.exit(f"no input device is named {name!r}")
	return found


def grab(name: str) -> None:
	device = devices(name)[0]
	device.grab()
	print("grabbed", flush=True)
	signal.sigwait({signal.SIGTERM, signal.SIGINT})


def leds(name: str) -> None:
	print(json.dumps({d.path: sum(1 << led for led in d.leds()) for d in devices(name)}))


def ptraceCall() -> ctypes.CDLL:
	libc = ctypes.CDLL(None, use_errno=True)
	libc.ptrace.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p]
	return libc


def stall(tid: int, seconds: float) -> None:
	libc = ptraceCall()
	for request in (PTRACE_SEIZE, PTRACE_INTERRUPT):
		if libc.ptrace(request, tid, None, None) != 0:
			sys.exit(f"ptrace {request:#x} of {tid}: errno {ctypes.get_errno()}")
	print(json.dumps({"stopped": time.monotonic()}), flush=True)
	# A traced thread that dies stays until its tracer has waited for it, and its parent, systemd,
	# hears of the process's end only then: it is waited for at once, so that systemd hears of it
	# when it would have without the tracer.
	deadline = time.monotonic() + seconds
	while time.monotonic() < deadline:
		waited, status = os.waitpid(tid, os.WNOHANG | WALL)
		if waited and (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
			print(json.dumps({"ended": time.monotonic()}), flush=True)
			return
		time.sleep(0.005)
	libc.ptrace(PTRACE_DETACH, tid, None, None)


def say(**fields: object) -> None:
	print(json.dumps({"t": time.monotonic(), **fields}), flush=True)


def race(mode: str, pid: int, name: str, key: str) -> None:
	keyboard = fakeKeyboard.make(name, "uhid")
	node = keyboard.node()
	code = ecodes.ecodes[key]
	say(step="made", node=node)
	libc = ptraceCall()
	if libc.ptrace(PTRACE_SEIZE, pid, None, ctypes.c_void_p(PTRACE_O_TRACESYSGOOD)) != 0:
		sys.exit(f"ptrace seize of {pid}: errno {ctypes.get_errno()}")
	libc.ptrace(PTRACE_INTERRUPT, pid, None, None)
	os.waitpid(pid, WALL)
	say(step="attached")
	registers = Registers()
	pressed = False
	deliver = 0
	while True:
		libc.ptrace(PTRACE_SYSCALL, pid, None, ctypes.c_void_p(deliver))
		_, status = os.waitpid(pid, WALL)
		if not os.WIFSTOPPED(status):
			say(step="ended")
			break
		stop, deliver = os.WSTOPSIG(status), 0
		if stop != signal.SIGTRAP | 0x80:
			if status >> 16 != PTRACE_EVENT_STOP:
				deliver = stop
			continue
		libc.ptrace(PTRACE_GETREGS, pid, None, ctypes.byref(registers))
		entering = registers.rax == 2**64 - ENOSYS
		if not (entering and registers.orig_rax == SYS_IOCTL):
			continue
		try:
			target = os.readlink(f"/proc/{pid}/fd/{registers.rdi}")
		except OSError:
			continue
		if target != node:
			continue
		request = registers.rsi & 0xFFFFFFFF
		if not pressed and request == EVIOCGRAB and registers.rdx == 1:
			keyboard.write(code, 1, [])
			pressed = True
			say(step="pressed before the grab")
		elif pressed and request == EVIOCGKEY:
			if mode == "across":
				keyboard.write(code, 0, [])
				say(step="released after the grab")
			libc.ptrace(PTRACE_DETACH, pid, None, None)
			say(step="detached")
			break
	fakeKeyboard.serve(keyboard)


if __name__ == "__main__":
	command, arguments = sys.argv[1:2], sys.argv[2:]
	if command == ["grab"] and len(arguments) == 1:
		grab(arguments[0])
	elif command == ["leds"] and len(arguments) == 1:
		leds(arguments[0])
	elif command == ["stall"] and len(arguments) == 2:
		stall(int(arguments[0]), float(arguments[1]))
	elif command == ["race"] and len(arguments) == 4 and arguments[0] in ("back", "across"):
		race(arguments[0], int(arguments[1]), arguments[2], arguments[3])
	else:
		sys.exit("usage: poke.py grab NAME | leds NAME | stall PID SECONDS | race back|across PID NAME KEY")
