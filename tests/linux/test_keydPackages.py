"""linda-keyd's packages for Debian and Ubuntu, Fedora and Arch Linux, read from the package files that
nix/keyd-packages.nix builds, without installing them: each holds the files the Meson install with
keyd_service lays down, where Meson lays them down with that distribution's library directory, with
the modes a package should give them, and a helper that needs no library of the distribution's.

The rig has no Nix, so the packages are built outside it and named to it with LINDA_KEYD_PACKAGES, the
output directories separated by colons:

	nix build --no-link --print-out-paths .#linda-keyd-deb .#linda-keyd-rpm .#linda-keyd-arch
	nix develop . -c env LINDA_KEYD_PACKAGES=DEB:RPM:ARCH tests/linux/rig --bind SCRATCH python3 -m pytest --basetemp=SCRATCH/NAME tests/linux/test_keydPackages.py

Without it the tests are skipped: nothing in the dev shell builds the packages, whose version moves
with every commit.
"""

import gzip
import io
import lzma
import os
import re
import struct
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import harness
import pytest

ROOT = harness.ROOT
PACKAGES_VARIABLE = "LINDA_KEYD_PACKAGES"
HELPER = "linda-keyd"
UNIT = "/usr/lib/systemd/system/linda-keyd.service"
#: Each format, by the name of its file, with the library directory Meson takes on its distributions.
FORMATS = {
	"deb": (".deb", "/usr/lib/x86_64-linux-gnu"),
	"rpm": (".rpm", "/usr/lib64"),
	"archlinux": (".pkg.tar.zst", "/usr/lib"),
}
#: What each format's package must ask for: systemd, polkit and the system bus.
DEPENDS = {
	"deb": ("systemd", "polkitd", "default-dbus-system-bus | dbus-system-bus"),
	"rpm": ("systemd", "polkit", "dbus"),
	"archlinux": ("systemd", "polkit", "dbus-units"),
}


@dataclass
class Package:
	path: Path
	#: The regular files it installs, by absolute path, with their modes and contents.
	files: dict[str, tuple[int, bytes]] = field(default_factory=dict)
	version: str = ""
	depends: list[str] = field(default_factory=list)
	#: Its maintainer scripts by the format's own names.
	scripts: dict[str, str] = field(default_factory=dict)


def _tarFiles(data: bytes) -> dict[str, tuple[int, bytes]]:
	files = {}
	with tarfile.open(fileobj=io.BytesIO(data)) as archive:
		for member in archive:
			if member.isfile():
				files["/" + member.name.removeprefix("./")] = (
					member.mode,
					archive.extractfile(member).read(),
				)
	return files


def _arMembers(data: bytes) -> dict[str, bytes]:
	assert data.startswith(b"!<arch>\n"), "not an ar archive"
	members, offset = {}, 8
	while offset < len(data):
		header = data[offset : offset + 60]
		name, size = header[:16].decode().strip().rstrip("/"), int(header[48:58])
		members[name] = data[offset + 60 : offset + 60 + size]
		offset += 60 + size + size % 2
	return members


def readDeb(path: Path) -> Package:
	members = _arMembers(path.read_bytes())
	dataName = next(name for name in members if name.startswith("data.tar"))
	controlName = next(name for name in members if name.startswith("control.tar"))
	package = Package(path, _tarFiles(members[dataName]))
	control = {
		Path(name).name: (mode, content) for name, (mode, content) in _tarFiles(members[controlName]).items()
	}
	fields = dict(re.findall(r"^(\S+): (.*)$", control["control"][1].decode(), re.MULTILINE))
	package.version = fields["Version"]
	package.depends = [d.strip() for d in fields.get("Depends", "").split(",") if d.strip()]
	for name in ("preinst", "postinst", "prerm", "postrm"):
		if name in control:
			mode, content = control[name]
			assert mode & 0o111, f"{name} is not executable"
			package.scripts[name] = content.decode()
	return package


#: The rpm header's tags read here.
RPM_TAGS = {
	1001: "version",
	1002: "release",
	1024: "postin",
	1025: "preun",
	1026: "postun",
	1030: "filemodes",
	1049: "requirename",
	1116: "dirindexes",
	1117: "basenames",
	1118: "dirnames",
	1125: "payloadcompressor",
}


def _rpmHeader(data: bytes, offset: int) -> tuple[dict[str, object], int]:
	"""The tags of the header at offset, and where the header ends."""
	assert data[offset : offset + 3] == b"\x8e\xad\xe8", "not an rpm header"
	count, size = struct.unpack(">II", data[offset + 8 : offset + 16])
	store = offset + 16 + count * 16
	tags: dict[str, object] = {}
	for i in range(count):
		tag, kind, start, n = struct.unpack(">IIII", data[offset + 16 + i * 16 : offset + 32 + i * 16])
		if tag not in RPM_TAGS:
			continue
		at = store + start
		if kind == 3:
			value: object = list(struct.unpack(f">{n}H", data[at : at + 2 * n]))
		elif kind == 4:
			value = list(struct.unpack(f">{n}I", data[at : at + 4 * n]))
		elif kind in (6, 8, 9):
			value = [s.decode() for s in data[at:].split(b"\0", n)[:n]]
			if kind == 6:
				value = value[0]
		else:
			continue
		tags[RPM_TAGS[tag]] = value
	return tags, store + size


def _cpioFiles(data: bytes) -> dict[str, bytes]:
	files, offset = {}, 0
	while True:
		assert data[offset : offset + 6] == b"070701", "not a newc cpio archive"
		fields = [int(data[offset + 6 + 8 * i : offset + 14 + 8 * i], 16) for i in range(13)]
		size, nameSize = fields[6], fields[11]
		name = data[offset + 110 : offset + 110 + nameSize - 1].decode()
		offset = (offset + 110 + nameSize + 3) & ~3
		if name == "TRAILER!!!":
			return files
		files["/" + name.removeprefix("./").lstrip("/")] = data[offset : offset + size]
		offset = (offset + size + 3) & ~3


def readRpm(path: Path) -> Package:
	data = path.read_bytes()
	# The lead, then the signature header padded to eight bytes, then the header.
	_, end = _rpmHeader(data, 96)
	tags, end = _rpmHeader(data, (end + 7) & ~7)
	payload = data[end:]
	compressor = tags.get("payloadcompressor", "gzip")
	if compressor == "gzip":
		payload = gzip.decompress(payload)
	elif compressor in ("xz", "lzma"):
		payload = lzma.decompress(payload)
	elif compressor == "zstd":
		payload = subprocess.run(["zstd", "-dc"], input=payload, capture_output=True, check=True).stdout
	contents = _cpioFiles(payload)
	package = Package(path, version=f"{tags['version']}-{tags['release']}", depends=list(tags["requirename"]))
	for directory, base, mode in zip(tags["dirindexes"], tags["basenames"], tags["filemodes"]):
		name = tags["dirnames"][directory] + base
		if mode & 0o170000 == 0o100000:
			package.files[name] = (mode & 0o7777, contents[name])
	for name in ("postin", "preun", "postun"):
		if name in tags:
			package.scripts[name] = tags[name]
	return package


def readArch(path: Path) -> Package:
	data = subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout
	allFiles = _tarFiles(data)
	package = Package(path, {name: value for name, value in allFiles.items() if not name.startswith("/.")})
	info = allFiles["/.PKGINFO"][1].decode()
	package.version = re.search(r"^pkgver = (.*)$", info, re.MULTILINE).group(1)
	package.depends = re.findall(r"^depend = (.*)$", info, re.MULTILINE)
	install = allFiles.get("/.INSTALL", (0, b""))[1].decode()
	for name, body in re.findall(r"^function (\w+)\(\) \{\n(.*?)\n\}$", install, re.MULTILINE | re.DOTALL):
		package.scripts[name] = body
	return package


READERS = {"deb": readDeb, "rpm": readRpm, "archlinux": readArch}


@pytest.fixture(scope="module")
def packages() -> dict[str, Package]:
	harness.requireRig()
	value = os.environ.get(PACKAGES_VARIABLE)
	if not value:
		pytest.skip(
			f"{PACKAGES_VARIABLE} is unset; build the packages with nix build .#linda-keyd-deb .#linda-keyd-rpm "
			".#linda-keyd-arch and name their directories in it, separated by colons"
		)
	found: dict[str, Path] = {}
	for entry in value.split(":"):
		entry = Path(entry)
		for path in entry.iterdir() if entry.is_dir() else [entry]:
			for format, (suffix, _) in FORMATS.items():
				if path.name.endswith(suffix):
					assert format not in found, f"{PACKAGES_VARIABLE} names two {format} packages"
					found[format] = path
	assert found.keys() == FORMATS.keys(), f"{PACKAGES_VARIABLE} names packages of {sorted(found)} alone"
	return {format: READERS[format](path) for format, path in found.items()}


@pytest.fixture(scope="module")
def mesonInstalls(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, tuple[int, bytes]]]:
	"""What the Meson install with keyd_service lays down under each format's library directory, but the
	three libraries, which stand in empty here as meson.build wants all four of prebuilt_libraries."""
	harness.requireRig()
	work = tmp_path_factory.mktemp("keydMeson")
	prebuilt = work / "prebuilt"
	prebuilt.mkdir()
	for name in ("libnvdaVbuf.so", "libpwsink.so", "libbre_vbuf.so", HELPER):
		(prebuilt / name).write_bytes(b"")
	installs = {}
	for format, (_, libDir) in FORMATS.items():
		build, destDir = work / f"build-{format}", work / f"dest-{format}"
		steps = [
			(
				"setup",
				str(build),
				str(ROOT),
				"--prefix=/usr",
				f"--libdir={libDir.removeprefix('/usr/')}",
				"-Dprogram=false",
				"-Dkeyd_service=true",
				f"-Dprebuilt_libraries={prebuilt}",
			),
			("install", "-C", str(build), "--destdir", str(destDir)),
		]
		for step in steps:
			done = subprocess.run(["meson", *step], capture_output=True, text=True, check=False, timeout=300)
			assert done.returncode == 0, (
				f"meson {step[0]} failed:\n{done.stdout[-6000:]}\n{done.stderr[-6000:]}"
			)
		installs[format] = {
			"/" + str(p.relative_to(destDir)): (p.stat().st_mode & 0o7777, p.read_bytes())
			for p in destDir.rglob("*")
			if p.is_file() and not p.name.endswith(".so")
		}
	return installs


@pytest.mark.parametrize("format", FORMATS)
def test_holdsWhatMesonInstallsWhereItInstallsIt(
	packages: dict[str, Package], mesonInstalls: dict[str, dict[str, tuple[int, bytes]]], format: str
):
	"""The same files at the same paths, the service's files byte for byte, so that the unit names the
	helper where this package puts it; the helper executable by all and the rest readable by all, and
	nothing writable but by root."""
	package, installed = packages[format], mesonInstalls[format]
	helper = f"{FORMATS[format][1]}/linda/{HELPER}"
	assert set(package.files) == set(installed), sorted(set(package.files) ^ set(installed))
	for name, (mode, content) in package.files.items():
		if name == helper:
			assert mode == 0o755, f"{name} has mode {mode:o}"
		else:
			assert mode == 0o644, f"{name} has mode {mode:o}"
			assert content == installed[name][1], f"{name} differs from what Meson installs"
	assert f"ExecStart={helper}" in package.files[UNIT][1].decode().splitlines()


def _elfStatic(content: bytes) -> list[str]:
	"""What keeps an x86_64 ELF executable from running without the distribution's loader and
	libraries: a program interpreter, or a shared library it names."""
	assert content[:4] == b"\x7fELF" and content[4] == 2, "not a 64-bit ELF file"
	assert struct.unpack_from("<H", content, 0x12)[0] == 62, "not for x86_64"
	phoff, phentsize, phnum = (
		struct.unpack_from("<Q", content, 0x20)[0],
		*struct.unpack_from("<HH", content, 0x36),
	)
	faults = []
	for i in range(phnum):
		kind, _, offset, _, _, size = struct.unpack_from("<IIQQQQ", content, phoff + i * phentsize)
		if kind == 3:
			faults.append("a program interpreter")
		elif kind == 2:
			for at in range(offset, offset + size, 16):
				if struct.unpack_from("<q", content, at)[0] == 1:
					faults.append("a shared library")
	return faults


@pytest.mark.parametrize("format", FORMATS)
def test_helperIsStatic(packages: dict[str, Package], format: str):
	_, content = packages[format].files[f"{FORMATS[format][1]}/linda/{HELPER}"]
	assert not _elfStatic(content)


def test_everyFormatHoldsTheSameHelperAndVersion(packages: dict[str, Package]):
	helpers = {packages[f].files[f"{FORMATS[f][1]}/linda/{HELPER}"][1] for f in FORMATS}
	assert len(helpers) == 1
	# Linda's version, then the commit's date and short hash; each format adds its release, 1.
	version = re.compile(r"(\d+\.\d+\.\d+)\+git\d{14}\.[0-9a-f]{7,}(\.dirty)?-1")
	versions = {f: packages[f].version for f in FORMATS}
	for format, value in versions.items():
		assert version.fullmatch(value), f"{format}: {value}"
	assert len(set(versions.values())) == 1, versions
	text = (ROOT / "source" / "buildVersion.py").read_text(encoding="utf-8")
	linda = ".".join(
		re.search(rf"^version_{n} = (\d+)", text, re.MULTILINE).group(1) for n in ("year", "major", "minor")
	)
	assert version.fullmatch(versions["deb"]).group(1) == linda


@pytest.mark.parametrize("format", FORMATS)
def test_dependsOnSystemdPolkitAndTheSystemBus(packages: dict[str, Package], format: str):
	for name in DEPENDS[format]:
		assert name in packages[format].depends, packages[format].depends


def test_debianStartsItOnInstallAndStopsItOnRemoval(packages: dict[str, Package]):
	scripts = packages["deb"].scripts
	assert "systemd-sysusers" in scripts["postinst"] and "linda-keyd.conf" in scripts["postinst"]
	assert "deb-systemd-helper enable linda-keyd.service" in scripts["postinst"]
	assert "action=start" in scripts["postinst"] and 'deb-systemd-invoke "$action"' in scripts["postinst"]
	assert "deb-systemd-invoke stop linda-keyd.service" in scripts["prerm"]


def test_fedoraPresetsItOnInstallAndStopsItOnRemoval(packages: dict[str, Package]):
	scripts = packages["rpm"].scripts
	assert "systemd-sysusers linda-keyd.conf" in scripts["postin"]
	assert "install-system-units linda-keyd.service" in scripts["postin"]
	assert "preset linda-keyd.service" in scripts["postin"]
	assert not re.search(r"systemctl .*(start|enable)", scripts["postin"])
	assert "remove-system-units linda-keyd.service" in scripts["preun"]
	assert "disable --now linda-keyd.service" in scripts["preun"]


def test_archLeavesItOffOnInstallAndStopsItOnRemoval(packages: dict[str, Package]):
	scripts = packages["archlinux"].scripts
	assert not re.search(r"^\s*systemctl", scripts["post_install"], re.MULTILINE)
	assert "disable --now linda-keyd.service" in scripts["pre_remove"]
