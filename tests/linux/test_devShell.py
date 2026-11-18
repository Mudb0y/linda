"""The dev shell: the variables it exports, the programs it leaves off PATH, and the native pieces it
names.

Linux code loads its native libraries and data by the absolute paths in LINDA_ variables, so a
variable that names nothing, or the wrong build, is a fault the code itself would report only as a
feature missing. PipeWire's daemon and session manager are named in variables rather than put on PATH,
so that tests start them only where they mean to. Run under the rig, which passes the shell's
LINDA_ variables in and gives its command the PATH the shell itself made:

	nix develop . -c tests/linux/rig python3 -m pytest tests/linux/test_devShell.py
"""

import importlib.machinery
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import harness
import pytest

ELF_MAGIC = b"\x7fELF"
STORE = "/nix/store/"
#: Sound and speech programs that must not be on the shell's PATH either, by name: each can reach a
#: sound server, a sound card or a screen reader.
OTHER_SOUND_PROGRAMS = (
	"pactl",
	"pacat",
	"paplay",
	"parec",
	"parecord",
	"pamon",
	"padsp",
	"pacmd",
	"aplay",
	"arecord",
	"amixer",
	"alsamixer",
	"alsaloop",
	"aplaymidi",
	"speaker-test",
	"play",
	"rec",
	"sox",
	"ffplay",
	"mpv",
	"mplayer",
	"vlc",
	"cvlc",
	"mpg123",
	"ogg123",
	"gst-launch-1.0",
	"gst-play-1.0",
	"canberra-gtk-play",
	"espeak",
	"espeak-ng",
	"spd-say",
	"spdsend",
	"speech-dispatcher",
	"orca",
)
#: Store names of packages whose programs play, record or route sound or speak, so that a program of
#: theirs on PATH is caught whatever it is called.
SOUND_PACKAGE = re.compile(
	r"[0-9a-z]{32}-(?:pipewire|wireplumber|pulseaudio|alsa-utils|alsa-tools|sox|ffmpeg|mpv|mplayer|vlc"
	r"|gstreamer|gst-plugins|jack|jack2|libcanberra|mpg123|vorbis-tools|sndio|portaudio|speechd"
	r"|speech-dispatcher|espeak|espeak-ng|flite|festival|orca)(?:-[a-z][a-z0-9]*)*-\d"
)
#: The three modules of openevv's NVDA driver, which the driver directory holds beside openevv_engine/.
OPENEVV_DRIVER_FILES = ("openevv.py", "_openevv.py", "_openevv_dictionaries.py")
OPENEVV_ENGINE = "openevv_engine/libeci.so.1"


def _directory(value: str) -> Path:
	path = Path(value)
	assert path.is_absolute() and path.is_dir(), f"{value} is not a directory"
	return path


def _elfFile(value: str) -> Path:
	path = Path(value)
	assert path.is_absolute() and path.is_file(), f"{value} is not a file"
	with open(path, "rb") as f:
		assert f.read(4) == ELF_MAGIC, f"{value} is not an ELF file"
	return path


def _holds(directory: Path, *names: str) -> None:
	missing = [name for name in names if not (directory / name).exists()]
	assert not missing, f"{directory} holds no {', '.join(missing)}"


def _program(directory: Path, name: str) -> None:
	path = directory / name
	assert path.is_file() and os.access(path, os.X_OK), f"{directory} holds no program {name}"


def _searchPath(value: str) -> None:
	entries = [entry for entry in value.split(":") if entry]
	assert entries, "the list is empty"
	missing = [entry for entry in entries if not os.path.isdir(entry)]
	assert not missing, f"no such directories: {missing}"


def _icuLibDir(value: str) -> None:
	_elfFile(str(_directory(value) / f"libicuuc.so.{os.environ['LINDA_ICU_VERSION_MAJOR']}"))


def _icuVersionMajor(value: str) -> None:
	# The library's own name carries it, and ICU suffixes every C symbol with it.
	assert re.fullmatch(r"[1-9]\d*", value), value
	_holds(Path(os.environ["LINDA_ICU_LIB_DIR"]), f"libicuuc.so.{value}")


def _louisTablesDir(value: str) -> None:
	# NVDA registers the last of these in mixed case, which liblouis does not ship.
	_holds(_directory(value), "en-us-g2.ctb", "unicode.dis", "No-No-g0.utb", "Se-Se.ctb")


def _liblouisVersion(value: str) -> None:
	assert re.fullmatch(r"\d+\.\d+\.\d+", value), value


def _cldrDir(value: str) -> None:
	_holds(_directory(value), "locale/en/cldr.dic", "locale/pl/cldr.dic")


def _espeakLib(value: str) -> None:
	assert _elfFile(value).name == "libespeak-ng.so.1"


def _espeakDataDir(value: str) -> None:
	directory = _directory(value)
	# eSpeak takes either the directory or its parent, and tells them apart by the name.
	assert directory.name == "espeak-ng-data", value
	_holds(directory, "phontab", "phondata", "en_dict", "pl_dict", "voices/!v", "lang")


def _openevvDriverDir(value: str) -> None:
	_holds(_directory(value), *OPENEVV_DRIVER_FILES, OPENEVV_ENGINE)


def _pipewireBin(value: str) -> None:
	_program(_directory(value), "pipewire")
	_program(_directory(value), "pw-cli")


def _wireplumberBin(value: str) -> None:
	_program(_directory(value), "wireplumber")


def _qt6PluginPath(value: str) -> None:
	# Without the xcb platform plugin a Qt program aborts before it opens a window.
	_holds(_directory(value), "platforms/libqxcb.so")


def _xorgBin(value: str) -> None:
	_program(_directory(value), "Xorg")
	_program(_directory(value), "Xephyr")


def _xorgModulePath(value: str) -> None:
	dummy, server = value.split(",")
	# The dummy video driver, and the server's own modules that it builds on.
	_holds(_directory(dummy), "drivers/dummy_drv.so")
	_holds(_directory(server), "libshadow.so")


def _mateDesktopSchemas(value: str) -> None:
	_holds(_directory(value), "org.mate.applications-at-visual.gschema.xml", "gschemas.compiled")


def _bash(value: str) -> None:
	path = _elfFile(value)
	assert path.name == "bash" and os.access(path, os.X_OK), value
	# Only a bash built with readline accepts emacs line editing.
	result = subprocess.run([value, "--norc", "-c", "set -o emacs"], capture_output=True, check=False)
	assert result.returncode == 0, result.stderr


def _brltty(value: str) -> None:
	path = _elfFile(value)
	assert path.name == "brltty" and os.access(path, os.X_OK), value


def _speechd(value: str) -> None:
	path = _elfFile(value)
	assert path.name == "speech-dispatcher" and os.access(path, os.X_OK), value
	# The configuration the test daemon is started with, the package's own and not the host's.
	assert (path.parent.parent / "etc/speech-dispatcher/speechd.conf").is_file(), value


def _mutter(value: str) -> None:
	path = _elfFile(value)
	assert path.name == "mutter" and os.access(path, os.X_OK), value


def _mutterSchemas(value: str) -> None:
	_holds(_directory(value), "glib-2.0/schemas/org.gnome.mutter.gschema.xml")


def _mesa(value: str) -> None:
	_holds(_directory(value), "share/glvnd/egl_vendor.d/50_mesa.json")


def _storePath(path: Path) -> Path:
	"""The store path a file is in."""
	return Path(STORE) / path.relative_to(STORE).parts[0]


def _pwsinkLib(value: str) -> None:
	assert _elfFile(value).name == "libpwsink.so", value


def _pwmon(value: str) -> None:
	path = _elfFile(value)
	assert path.name == "pwmon" and os.access(path, os.X_OK), value
	# The program's package takes the library's store path, which must not hold a program that
	# records sound.
	assert _storePath(path) != _storePath(Path(os.environ["LINDA_PWSINK_LIB"])), value


def _vbufLib(value: str) -> None:
	assert _elfFile(value).name == "libnvdaVbuf.so", value


def _vbufEngine(value: str) -> None:
	assert _elfFile(value).name == "libbre_vbuf.so", value


def _cargoVendorDir(value: str) -> None:
	# Every crate the engine's lock file names, as cargo vendor lays them out, for the Meson build.
	directory = _directory(value)
	with open(harness.ROOT / "engine" / "Cargo.lock", "rb") as f:
		packages = tomllib.load(f)["package"]
	crates = {f"{p['name']}-{p['version']}" for p in packages if p.get("source", "").startswith("registry+")}
	_holds(directory, *sorted(crates))


def _pkgConfigPath(value: str) -> None:
	_searchPath(value)
	for name in ("libpipewire-0.3", "atspi-2", "icu-uc", "liblouis"):
		assert any(os.path.isfile(os.path.join(d, f"{name}.pc")) for d in value.split(":")), name


def _benchBin(value: str) -> None:
	# Named without depending on the bench, so that entering the shell never builds the engine: it
	# exists once nix build .#linda-bench has built it.
	assert re.fullmatch(r"/nix/store/[0-9a-z]{32}-linda-bench-[^/]+/bin/linda-bench", value), value


#: Every LINDA_ variable the shell exports, with what makes its value right.
SHELL_VARIABLES: dict[str, Callable[[str], None]] = {
	"LINDA_ICU_LIB_DIR": _icuLibDir,
	"LINDA_ICU_VERSION_MAJOR": _icuVersionMajor,
	"LINDA_LOUIS_TABLES_DIR": _louisTablesDir,
	"LINDA_LIBLOUIS_VERSION": _liblouisVersion,
	"LINDA_NVDA_CLDR_DIR": _cldrDir,
	"LINDA_ESPEAK_LIB": _espeakLib,
	"LINDA_ESPEAK_DATA_DIR": _espeakDataDir,
	"LINDA_OPENEVV_DRIVER_DIR": _openevvDriverDir,
	"LINDA_PIPEWIRE_BIN": _pipewireBin,
	"LINDA_WIREPLUMBER_BIN": _wireplumberBin,
	"LINDA_PWSINK_LIB": _pwsinkLib,
	"LINDA_PWMON": _pwmon,
	"LINDA_VBUF_LIB": _vbufLib,
	"LINDA_VBUF_ENGINE": _vbufEngine,
	"LINDA_BENCH_BIN": _benchBin,
	"LINDA_CARGO_VENDOR_DIR": _cargoVendorDir,
	"LINDA_PKG_CONFIG_PATH": _pkgConfigPath,
	"LINDA_QT6_PLUGIN_PATH": _qt6PluginPath,
	"LINDA_XORG_BIN": _xorgBin,
	"LINDA_XORG_MODULE_PATH": _xorgModulePath,
	"LINDA_MATE_DESKTOP_SCHEMAS": _mateDesktopSchemas,
	"LINDA_BASH": _bash,
	"LINDA_BRLTTY": _brltty,
	"LINDA_SPEECHD": _speechd,
	"LINDA_MUTTER": _mutter,
	"LINDA_MUTTER_SCHEMAS": _mutterSchemas,
	"LINDA_MESA": _mesa,
	"LINDA_RIG_PATH": _searchPath,
	"LINDA_RIG_XDG_DATA_DIRS": _searchPath,
}


@pytest.mark.parametrize("name", SHELL_VARIABLES)
def test_shellVariableNamesWhatItShould(name: str):
	value = os.environ.get(name)
	assert value, f"{name} is not set; the dev shell exports it"
	SHELL_VARIABLES[name](value)


def test_everyVariableNamingTheStoreIsChecked():
	"""A variable the shell exports names a store path, which a run's own settings do not; one that is
	not in SHELL_VARIABLES was added to the shell without saying what makes it right."""
	unchecked = sorted(
		name
		for name, value in os.environ.items()
		if name.startswith("LINDA_") and STORE in value and name not in SHELL_VARIABLES
	)
	assert not unchecked, f"add these to SHELL_VARIABLES: {unchecked}"


def test_liblouisVersionIsTheLibraryNvdaLoads(tmp_path: Path):
	done = harness.runChild(tmp_path, "import louis\nresult = louis.version()", prepare="none")
	done.assertClean()
	assert done.value == os.environ["LINDA_LIBLOUIS_VERSION"]


@pytest.mark.parametrize("searchPath", ["PATH", "LINDA_RIG_PATH"])
def test_noPipewireOrWireplumberProgramIsOnPath(searchPath: str):
	"""Not one of the programs PipeWire's and WirePlumber's packages install, nothing from a directory of
	a package that plays, records or routes sound or speaks, and none of OTHER_SOUND_PROGRAMS.

	So the tests reach PipeWire only through LINDA_PIPEWIRE_BIN, and a host without PipeWire's
	tools gets none from the shell. Firefox is on PATH for the browser tests and can play sound; the rig
	gives it no sound server. Inside the rig PATH is the store part of LINDA_RIG_PATH, which is the
	shell's PATH without what nix develop appended from the caller's, so this is what the shell itself
	put there."""
	directories = [entry for entry in os.environ[searchPath].split(":") if entry]
	fromSoundPackages = [
		entry
		for entry in directories
		if entry.startswith(STORE) and SOUND_PACKAGE.match(entry.removeprefix(STORE).split("/")[0])
	]
	assert not fromSoundPackages, f"on {searchPath}: {fromSoundPackages}"
	programs = set(OTHER_SOUND_PROGRAMS)
	for variable in ("LINDA_PIPEWIRE_BIN", "LINDA_WIREPLUMBER_BIN"):
		programs.update(os.listdir(os.environ[variable]))
	found = {
		name: where for name in sorted(programs) if (where := shutil.which(name, path=os.environ[searchPath]))
	}
	assert not found, f"on {searchPath}: {found}"


#: Loads the library SYNTH_LIBRARY names, and reports what that mapped that could sound, and every
#: shared object mapped, for the failure message.
LOAD_LIBRARY = """
import ctypes
import os

path = os.path.realpath(os.environ["SYNTH_LIBRARY"])
ctypes.CDLL(path)
with open("/proc/self/maps", encoding="utf-8") as maps:
	mapped = {fields[5].strip() for line in maps if len(fields := line.split(None, 5)) == 6}
result = {
	"sound": [found for found in harness.soundReached(allowLinked=False) if found != f"mapped {path}"],
	"mapped": sorted(name for name in mapped if ".so" in name),
}
"""


@pytest.mark.parametrize(
	("variable", "relative"),
	[("LINDA_ESPEAK_LIB", ""), ("LINDA_OPENEVV_DRIVER_DIR", OPENEVV_ENGINE)],
)
def test_synthesizerLibraryNeedsNoSoundLibrary(tmp_path: Path, variable: str, relative: str):
	"""Loaded alone, the library maps nothing that can reach a sound server or a sound card, through any
	library it needs: synthesizers hand their audio to NVDA's player and play nothing themselves."""
	env = {"SYNTH_LIBRARY": os.path.join(os.environ[variable], relative)}
	done = harness.runChild(tmp_path, LOAD_LIBRARY, prepare="none", env=env)
	done.assertClean()
	assert done.value["sound"] == [], "\n".join(done.value["mapped"])


PT_LOAD = 1
PT_DYNAMIC = 2
DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_RPATH = 15
DT_RUNPATH = 29


def _dynamicStrings(path: Path) -> dict[int, list[str]]:
	"""The string entries of a 64-bit little-endian ELF file's dynamic section, by tag."""
	data = path.read_bytes()
	assert data[4:6] == b"\x02\x01", f"{path} is not a 64-bit little-endian ELF file"
	(programHeaders,) = struct.unpack_from("<Q", data, 0x20)
	entrySize, count = struct.unpack_from("<HH", data, 0x36)
	# Each as p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align.
	segments = [struct.unpack_from("<IIQQQQQQ", data, programHeaders + i * entrySize) for i in range(count)]
	(dynamic,) = [s for s in segments if s[0] == PT_DYNAMIC]
	entries = []
	for at in range(dynamic[2], dynamic[2] + dynamic[5], 16):
		tag, value = struct.unpack_from("<qQ", data, at)
		if tag == DT_NULL:
			break
		entries.append((tag, value))
	# The string table is named by its address once loaded, which a loaded segment maps to the file.
	address = next(value for tag, value in entries if tag == DT_STRTAB)
	(table,) = [s[2] + address - s[3] for s in segments if s[0] == PT_LOAD and s[3] <= address < s[3] + s[5]]
	strings: dict[int, list[str]] = {}
	for tag, value in entries:
		if tag in (DT_NEEDED, DT_RPATH, DT_RUNPATH):
			start = table + value
			strings.setdefault(tag, []).append(data[start : data.index(b"\0", start)].decode())
	return strings


def test_pwsinkNeedsOnlyPipewiresClientFromTheStore(tmp_path: Path):
	"""libpwsink finds the libraries it needs only in the store, whatever the caller's directory or
	loader path holds; the one sound library it needs, and the only one loading it maps, is
	PipeWire's client library; and nothing in its store path names pwmon's, so a package that holds
	the library does not hold the recorder."""
	library = _elfFile(os.environ["LINDA_PWSINK_LIB"])
	strings = _dynamicStrings(library)
	searched = [
		d for tag in (DT_RPATH, DT_RUNPATH) for value in strings.get(tag, []) for d in value.split(":")
	]
	assert searched, f"{library} has no RUNPATH, so the loader would look for its libraries elsewhere"
	assert all(d.startswith(STORE) for d in searched), searched
	needed = strings.get(DT_NEEDED, [])
	assert [name for name in needed if harness.isAudioLibrary(name)] == ["libpipewire-0.3.so.0"], needed
	done = harness.runChild(
		tmp_path,
		"""
		import ctypes
		import os

		ctypes.CDLL(os.environ["LINDA_PWSINK_LIB"])
		result = harness.soundReached(allowLinked=False)
		""",
		prepare="none",
	)
	done.assertClean(ignoring={"audio"})
	pipewireLib = Path(os.path.realpath(os.environ["LINDA_PIPEWIRE_BIN"])).parent / "lib"
	mapped = [what.removeprefix("mapped ") for what in done.value]
	assert len(mapped) == len(done.value) == 1, done.value
	assert Path(mapped[0]).parent == pipewireLib and Path(mapped[0]).name.startswith("libpipewire-0.3.so"), (
		mapped
	)
	assert done.audio == done.value, done.audio
	# How Nix itself finds a store path's references.
	pwmonHash = _storePath(Path(os.environ["LINDA_PWMON"])).name[:32].encode()
	naming = [
		str(path)
		for path in _storePath(library).rglob("*")
		if path.is_file() and not path.is_symlink() and pwmonHash in path.read_bytes()
	]
	assert not naming, naming


def _loadTool(name: str) -> ModuleType:
	"""A script in tools/, which has no .py suffix, loaded as a module without running it."""
	loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(harness.ROOT / "tools" / name))
	module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
	loader.exec_module(module)
	return module


def test_espeakAndSonicAreTheCommitsUpstreamPins(monkeypatch: pytest.MonkeyPatch):
	"""nix/espeak-ng.nix fetches the commits upstream's include/espeak and include/sonic submodule links
	name, read as tools/check-upstream-edits reads them, and the library the shell names was built from
	that eSpeak commit, which the derivation's name records.

	Each is fetched with builtins.fetchTree, which fetches the rev and refuses a tree whose hash is not
	narHash. A fixed-output fetcher is found by its hash alone, so with one a rev moved without its hash
	would name the new commit and build the old."""
	monkeypatch.chdir(harness.ROOT)
	tool = _loadTool("check-upstream-edits")
	base = subprocess.run(
		[harness.ROOT / "tools" / "upstream-ref"], capture_output=True, text=True, check=True
	).stdout.strip()
	pins = {
		repo: tool.objectAt(base, link)
		for link, nixFile, repo in tool.NIX_PINS
		if nixFile == "nix/espeak-ng.nix"
	}
	assert set(pins) == {"espeak-ng", "sonic"}
	assert all(re.fullmatch(r"[0-9a-f]{40}", pin or "") for pin in pins.values()), pins
	errors = [error for error in tool.checkNixPins(base) if any(f" {repo} " in error for repo in pins)]
	assert not errors, errors
	nix = (harness.ROOT / "nix" / "espeak-ng.nix").read_text(encoding="utf-8")
	for repo, pin in pins.items():
		fetch = (
			rf'src = builtins\.fetchTree \{{\s*type = "github";\s*owner = "[\w-]+";\s*repo = "{repo}";'
			rf'\s*rev = "{pin}";\s*narHash = "sha256-[\w+/=]+";\s*\}};'
		)
		assert re.search(fetch, nix), f"nix/espeak-ng.nix does not fetch {repo} with builtins.fetchTree"
	library = Path(os.environ["LINDA_ESPEAK_LIB"])
	storeName = library.relative_to(STORE).parts[0]
	assert storeName.endswith(f"-unstable-{pins['espeak-ng'][:8]}"), storeName


def test_openevvDriverDirectoryHoldsTheDriverAndTheEngine():
	"""The layout of the driver's add-on on Windows: the three modules beside openevv_engine/, where
	the driver looks for the engine library on every platform, a link to the built library."""
	directory = _directory(os.environ["LINDA_OPENEVV_DRIVER_DIR"])
	assert sorted(entry.name for entry in directory.iterdir()) == sorted(
		[*OPENEVV_DRIVER_FILES, "openevv_engine"]
	)
	for name in OPENEVV_DRIVER_FILES:
		assert (directory / name).is_file() and not (directory / name).is_symlink(), name
	engine = directory / OPENEVV_ENGINE
	assert sorted(entry.name for entry in engine.parent.iterdir()) == ["libeci.so.1"]
	assert engine.is_symlink()
	target = os.readlink(engine)
	assert target.startswith(STORE) and target.endswith("/lib/libeci.so.1"), target
	_elfFile(target)
