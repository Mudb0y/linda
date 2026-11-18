"""The Meson build as a distribution would run it: configured with prefix /usr, built, installed into a
DESTDIR, and the installed copy started from there.

The build takes what the dev shell names as its options: the CLDR data, NVDA's pinned eSpeak NG, ICU's
library directory and openevv's driver. Nothing is compiled in the rig, which has no compilers'
environment, so the three native libraries are the dev shell's, which the same build made with
program=false, installed through prebuilt_libraries; the rest of the build, the catalogues, the CLDR
dictionaries and the braille tables, runs here. The start unsets every LINDA_ variable
an installation fixes, so that the installed copy finds its pieces through linuxPlatform.installPaths,
and names only the three libraries, which an installation keeps under /usr, where the rig has none of
them. It speaks through eSpeak NG on the PipeWire fixture.

	nix develop . -c tests/linux/rig --bind SCRATCH python3 -m pytest --basetemp=SCRATCH/NAME tests/linux/test_mesonInstall.py
"""

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import harness
import pytest
from pipewireFixture import PipewireFixture

ROOT = harness.ROOT
PREFIX = "/usr"
#: What the build's options take from the dev shell.
OPTIONS = {
	"cldr_data": "LINDA_NVDA_CLDR_DIR",
	"espeak_library": "LINDA_ESPEAK_LIB",
	"espeak_data": "LINDA_ESPEAK_DATA_DIR",
	"icu_lib_dir": "LINDA_ICU_LIB_DIR",
	"openevv_driver": "LINDA_OPENEVV_DRIVER_DIR",
}
#: Each library, by the variable the dev shell names it with.
LIBRARIES = {
	"libnvdaVbuf.so": "LINDA_VBUF_LIB",
	"libpwsink.so": "LINDA_PWSINK_LIB",
	"libbre_vbuf.so": "LINDA_VBUF_ENGINE",
}
#: Seconds for a whole build; it takes about twenty.
BUILD_BOUND = 600
STARTUP_BOUND = 60.0
SHUTDOWN_BOUND = 20.0


def meson(
	*arguments: str, env: dict[str, str] | None = None, timeout: float = BUILD_BOUND
) -> subprocess.CompletedProcess:
	return subprocess.run(
		["meson", *arguments],
		capture_output=True,
		text=True,
		check=False,
		timeout=timeout,
		env={**os.environ, "PKG_CONFIG_PATH": os.environ["LINDA_PKG_CONFIG_PATH"], **(env or {})},
	)


def options(work: Path, *leftOut: str) -> list[str]:
	"""The build's options as the dev shell gives them, but those named in leftOut."""
	libraries = work / "libraries"
	if not libraries.is_dir():
		libraries.mkdir()
		for name, variable in LIBRARIES.items():
			(libraries / name).symlink_to(os.environ[variable])
	given = [f"-D{name}={os.environ[variable]}" for name, variable in OPTIONS.items() if name not in leftOut]
	return [f"-Dprebuilt_libraries={libraries}", *given]


@dataclass
class Installed:
	build: Path
	destDir: Path
	libDir: str

	def path(self, installed: str) -> Path:
		"""Where a path the installation names is in the DESTDIR."""
		return self.destDir / installed.lstrip("/")

	@property
	def appDir(self) -> Path:
		return self.path(f"{PREFIX}/share/linda/source")

	def installPaths(self) -> dict[str, str]:
		text = (self.appDir / "linuxPlatform" / "installPaths.py").read_text(encoding="utf-8")
		namespace: dict = {}
		# The module Meson wrote a moment ago from linux/installPaths.py.in.
		exec(compile(text, "installPaths.py", "exec"), namespace)  # noqa: S102
		return namespace["values"]


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Installed:
	harness.requireRig()
	work = tmp_path_factory.mktemp("meson")
	build = work / "build"
	destDir = work / "dest"
	steps = [
		("setup", str(build), str(ROOT), f"--prefix={PREFIX}", *options(work)),
		("compile", "-C", str(build)),
		("install", "-C", str(build), "--destdir", str(destDir)),
	]
	for step in steps:
		done = meson(*step)
		assert done.returncode == 0, f"meson {step[0]} failed:\n{done.stdout[-6000:]}\n{done.stderr[-6000:]}"
	introspected = json.loads(meson("introspect", "--buildoptions", str(build)).stdout)
	libDir = next(option["value"] for option in introspected if option["name"] == "libdir")
	return Installed(build, destDir, f"{PREFIX}/{libDir}")


def test_installsTheLayoutAndNothingElse(installed: Installed):
	"""Every file outside the Python tree, by name, and the Python tree with what the build made."""
	expected = {
		f"{PREFIX}/bin/linda",
		f"{PREFIX}/bin/linda-say",
		f"{PREFIX}/share/linda/copying.txt",
		f"{PREFIX}/share/licenses/linda/copying.txt",
		f"{PREFIX}/share/applications/linda.desktop",
		f"{PREFIX}/share/metainfo/io.github.mudb0y.linda.metainfo.xml",
		f"{PREFIX}/share/man/man1/linda.1",
		f"{PREFIX}/share/man/man1/linda-say.1",
		*(f"{installed.libDir}/linda/{name}" for name in LIBRARIES),
	}
	found = set()
	appDir = installed.appDir
	for directory, names, files in os.walk(installed.destDir):
		if Path(directory) == appDir.parent:
			names.remove("source")
		found.update("/" + str((Path(directory) / name).relative_to(installed.destDir)) for name in files)
	assert found == expected
	for name in ("linda", "linda-say"):
		assert os.access(installed.path(f"{PREFIX}/bin/{name}"), os.X_OK), name
	for name, variable in LIBRARIES.items():
		path = installed.path(f"{installed.libDir}/linda/{name}")
		assert not path.is_symlink() and path.read_bytes() == Path(os.environ[variable]).read_bytes(), name
	for relative in (
		"linda.py",
		"linuxPlatform/installPaths.py",
		"locale/pl/LC_MESSAGES/nvda.mo",
		"locale/pl/cldr.dic",
		"locale/pl/symbols.dic",
		"locale/pt_PT/cldr.dic",
		"locale/sq/LC_MESSAGES/nvda.mo",
		"louis/tables/en-us-g2.ctb",
		"louis/tables/No-No-g0.utb",
		"waves/start.wav",
		"images",
	):
		assert (appDir / relative).exists(), relative
	# documentationUtils.getDocFilePath's place for the licence when NVDA runs from source, as it does
	# on Linux: beside the program's folder.
	assert (appDir / ".." / "copying.txt").read_bytes() == (ROOT / "copying.txt").read_bytes()
	# Nothing generated in the checkout comes along: the build directory is where it was made.
	assert not (ROOT / "source" / "linuxPlatform" / "installPaths.py").exists()


def test_everyModuleIsCompiledForTheInstalledPath(installed: Installed):
	"""The program runs with -B from a directory it cannot write, so a module without its compiled
	file would be compiled on every start. Each is checked by hash alone and names its installed path."""
	modules = sorted(installed.appDir.rglob("*.py"))
	assert len(modules) > 600
	uncompiled = [
		str(module.relative_to(installed.appDir))
		for module in modules
		if not (module.parent / "__pycache__" / f"{module.stem}.cpython-313.pyc").is_file()
	]
	assert not uncompiled, uncompiled
	compiled = (
		installed.appDir / "linuxPlatform" / "__pycache__" / "installSettings.cpython-313.pyc"
	).read_bytes()
	# The flags word: checked by hash, without checking the source.
	assert int.from_bytes(compiled[4:8], "little") == 0b01
	assert f"{PREFIX}/share/linda/source/linuxPlatform/installSettings.py".encode() in compiled
	assert str(installed.destDir).encode() not in compiled


def test_launchersStartPythonOnTheInstalledProgram(installed: Installed):
	python = os.path.realpath(shutil.which("python3.13"))
	for name, arguments in (("linda", ""), ("linda-say", " say")):
		text = installed.path(f"{PREFIX}/bin/{name}").read_text(encoding="utf-8")
		lines = text.splitlines()
		assert lines[0] == "#!/bin/sh", text
		match = re.fullmatch(r"exec '([^']+)' -I -B '([^']+)'(.*) \"\$@\"", lines[1])
		assert match and len(lines) == 2, text
		assert os.path.realpath(match[1]) == python, text
		assert match[2] == f"{PREFIX}/share/linda/source/linda.py", text
		assert match[3] == arguments, text


def test_installPathsNameWhatTheBuildFoundAndBuilt(installed: Installed):
	values = installed.installPaths()
	assert values == {
		"LINDA_VBUF_LIB": f"{installed.libDir}/linda/libnvdaVbuf.so",
		"LINDA_VBUF_ENGINE": f"{installed.libDir}/linda/libbre_vbuf.so",
		"LINDA_PWSINK_LIB": f"{installed.libDir}/linda/libpwsink.so",
		"LINDA_AUDIO_SINK": "pipewire",
		"LINDA_ESPEAK_LIB": os.environ["LINDA_ESPEAK_LIB"],
		"LINDA_ESPEAK_DATA_DIR": os.environ["LINDA_ESPEAK_DATA_DIR"],
		"LINDA_ICU_LIB_DIR": os.environ["LINDA_ICU_LIB_DIR"],
		"LINDA_ICU_VERSION_MAJOR": os.environ["LINDA_ICU_VERSION_MAJOR"],
		"LINDA_OPENEVV_DRIVER_DIR": os.environ["LINDA_OPENEVV_DRIVER_DIR"],
	}


def installedEnvironment(installed: Installed) -> dict[str, str]:
	"""The rig's environment without anything that names a piece the installation fixes, and with only
	the three libraries named, which the installation keeps under /usr, where the rig has nothing."""
	values = installed.installPaths()
	environment = {
		name: value
		for name, value in os.environ.items()
		if name not in values and name not in ("LINDA_TEST_HOOKS_DIR", "LINDA_AUDIO_CAPTURE_DIR")
	}
	for name in ("LINDA_VBUF_LIB", "LINDA_VBUF_ENGINE", "LINDA_PWSINK_LIB"):
		environment[name] = str(installed.path(values[name]))
	return environment


def launcher(installed: Installed, name: str, directory: Path) -> list[str]:
	"""The installed launcher, with its program's path moved into the DESTDIR."""
	text = installed.path(f"{PREFIX}/bin/{name}").read_text(encoding="utf-8")
	moved = directory / name
	moved.write_text(text.replace(f"'{PREFIX}/share/linda/", f"'{installed.destDir}{PREFIX}/share/linda/"))
	return ["sh", str(moved)]


#: A first start's configuration, as test_boot's but speaking with eSpeak NG.
CONFIG = (
	"schemaVersion = 25\n"
	"[general]\n\tlanguage = en\n\tplayStartAndExitSounds = False\n"
	"[speech]\n\tsynth = espeak\n"
	"[braille]\n\tdisplay = noBraille\n"
	"[addonStore]\n\tautomaticUpdates = disabled\n\tbaseServerURL = http://127.0.0.1:1\n"
)


def test_installedCopyStartsSpeaksAndExits(installed: Installed, pipewire: PipewireFixture, tmp_path: Path):
	monitor = pipewire.monitor()
	config = tmp_path / "config"
	config.mkdir()
	(config / "nvda.ini").write_text(CONFIG, encoding="utf-8")
	log = tmp_path / "nvda.log"
	environment = installedEnvironment(installed)
	before = harness.treeState(installed.destDir)
	started = time.monotonic_ns()
	with open(tmp_path / "stderr.txt", "w") as stderr:
		process = subprocess.Popen(
			[*launcher(installed, "linda", tmp_path), "-c", str(config), "-f", str(log), "--debug-logging"],
			env=environment,
			stdout=stderr,
			stderr=stderr,
		)
	try:

		def describe() -> str:
			text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
			return f"{harness.describeExit(process.poll())}\n{(tmp_path / 'stderr.txt').read_text()}\n{text[-6000:]}"

		deadline = time.monotonic() + STARTUP_BOUND
		spoken: list[str] = []
		while not spoken:
			assert process.poll() is None and time.monotonic() < deadline, f"nothing was spoken\n{describe()}"
			time.sleep(0.1)
			if log.exists():
				spoken = re.findall(r"^Speaking \[(.*)\]$", log.read_text(encoding="utf-8"), re.MULTILINE)
		heard = monitor.soundAfter(started, timeout=STARTUP_BOUND)
		assert heard >= started
		quitter = subprocess.run(
			[*launcher(installed, "linda", tmp_path), "--quit"],
			env=environment,
			capture_output=True,
			text=True,
			timeout=SHUTDOWN_BOUND,
			check=False,
		)
		assert quitter.returncode == 0, quitter
		assert process.wait(SHUTDOWN_BOUND) == 0, describe()
	finally:
		if process.poll() is None:
			process.kill()
			process.wait()
	text = log.read_text(encoding="utf-8")
	assert not harness.nvdaLogErrorRecords(text), text[-8000:]
	for name in ("LINDA_ESPEAK_LIB", "LINDA_ICU_LIB_DIR", "LINDA_OPENEVV_DRIVER_DIR"):
		assert name not in environment
	assert "Loaded synthDriver espeak" in text, text[-8000:]
	assert harness.changedPaths(before, harness.treeState(installed.destDir)) == []


def test_installedSayIsHeardThroughPipewire(installed: Installed, pipewire: PipewireFixture, tmp_path: Path):
	monitor = pipewire.monitor()
	started = time.monotonic_ns()
	done = subprocess.run(
		[*launcher(installed, "linda-say", tmp_path), "--factory", "--synth", "espeak", "Linda, installed."],
		env=installedEnvironment(installed),
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)
	assert done.returncode == 0, done
	assert "espeak" in done.stderr and "through PipeWire" in done.stderr, done.stderr
	assert monitor.soundAfter(started) >= started


def test_systemEspeakIsTheLibraryBySonameAndItsOwnData(tmp_path: Path):
	"""Without options naming eSpeak NG, the build takes the one pkg-config finds, loaded by its soname
	with its own data, and says that no test run has proven it. A fake installation stands in for a
	distribution's, with its data where Debian keeps it."""
	fake = tmp_path / "espeak"
	data = fake / "lib" / "espeak-ng-data"
	(data / "voices" / "!v").mkdir(parents=True)
	(fake / "lib" / "pkgconfig").mkdir()
	(fake / "lib" / "pkgconfig" / "espeak-ng.pc").write_text(
		f"prefix={fake}\nlibdir=${{prefix}}/lib\nincludedir=${{prefix}}/include\n\n"
		"Name: espeak-ng\nDescription: a stand-in\nVersion: 1.52.0\nLibs: -L${libdir} -lespeak-ng\n",
		encoding="utf-8",
	)
	done = meson(
		"setup",
		str(tmp_path / "build"),
		str(ROOT),
		f"--prefix={PREFIX}",
		*options(tmp_path, "espeak_library", "espeak_data"),
		env={"PKG_CONFIG_PATH": f"{fake}/lib/pkgconfig:{os.environ['LINDA_PKG_CONFIG_PATH']}"},
		timeout=300,
	)
	assert done.returncode == 0, done.stdout[-6000:] + done.stderr[-6000:]
	assert "eSpeak NG is the system's 1.52.0, which no test run has proven" in done.stdout, done.stdout
	text = (tmp_path / "build" / "installPaths.py").read_text(encoding="utf-8")
	assert '"LINDA_ESPEAK_LIB": "libespeak-ng.so.1",' in text
	assert f'"LINDA_ESPEAK_DATA_DIR": "{data}",' in text


@pytest.mark.parametrize(
	("option", "value", "message"),
	[
		("cldr_data", "", "-Dcldr_data must name a checkout of nvaccess/nvda-cldr"),
		("espeak_library", "/nonexistent/libespeak-ng.so.1", "-Despeak_library names /nonexistent"),
		("openevv_driver", "/nonexistent", "-Dopenevv_driver names /nonexistent, which holds no openevv.py"),
		("icu_lib_dir", "/nonexistent", "/nonexistent holds no libicuuc.so."),
	],
)
def test_configureNamesWhatIsMissing(tmp_path: Path, option: str, value: str, message: str):
	given = options(tmp_path, option)
	done = meson("setup", str(tmp_path / "build"), str(ROOT), *given, f"-D{option}={value}", timeout=300)
	assert done.returncode != 0
	assert message in done.stdout, done.stdout[-3000:]


def test_desktopEntryAndMetadataSayWhatLindaIs(installed: Installed):
	"""Both validate, the entry starts linda with no icon of NVDA's, and the metadata says what the
	readme says of NVDA and NV Access."""
	done = meson("test", "-C", str(installed.build), "--suite", "files", "--print-errorlogs")
	assert done.returncode == 0, done.stdout[-6000:]
	assert re.search(r"^Ok:\s+2\s*$", done.stdout, re.MULTILINE), done.stdout
	entry = installed.path(f"{PREFIX}/share/applications/linda.desktop").read_text(encoding="utf-8")
	lines = entry.splitlines()
	for line in ("Type=Application", "Name=Linda", "Exec=linda", "Categories=Utility;Accessibility;"):
		assert line in lines, entry
	assert any(line.startswith("Comment=") for line in lines), entry
	assert not any(line.startswith("Icon=") for line in lines), entry
	metainfo = installed.path(f"{PREFIX}/share/metainfo/io.github.mudb0y.linda.metainfo.xml").read_text(
		encoding="utf-8"
	)
	description = " ".join(metainfo.split())
	assert "<id>io.github.mudb0y.linda</id>" in description
	assert '<launchable type="desktop-id">linda.desktop</launchable>' in description
	readme = (ROOT / "readme.md").read_text(encoding="utf-8")
	assert "is not affiliated with or endorsed by NV Access." in readme
	assert "It is an independent project and is not affiliated with or endorsed by NV Access." in description
	assert "based on NVDA by NV Access and the NVDA contributors" in description
	assert (installed.path(f"{PREFIX}/share/licenses/linda/copying.txt")).read_bytes() == (
		ROOT / "copying.txt"
	).read_bytes()


def test_manualPagesNameEveryOptionTheHelpDoes(installed: Installed, tmp_path: Path):
	"""Each option linda --help and linda say --help name is in the manual page for it."""
	environment = installedEnvironment(installed)
	for page, arguments in (("linda.1", ["--help"]), ("linda-say.1", ["say", "--help"])):
		name = "linda" if page == "linda.1" else "linda-say"
		shown = subprocess.run(
			[*launcher(installed, "linda", tmp_path), *arguments],
			env=environment,
			capture_output=True,
			text=True,
			timeout=60,
			check=False,
		)
		text = shown.stdout + shown.stderr
		options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", text))
		assert len(options) > 5, text
		manual = (
			installed.path(f"{PREFIX}/share/man/man1/{page}").read_text(encoding="utf-8").replace("\\-", "-")
		)
		assert manual.startswith(f".TH {name.upper()} 1 "), manual[:80]
		# NVDA's help mentions --loglevel in passing, a spelling no option has.
		missing = sorted(option for option in options - {"--loglevel"} if option not in manual)
		assert not missing, (page, missing)
