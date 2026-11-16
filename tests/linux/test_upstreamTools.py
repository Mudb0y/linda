"""The upstream-tracking tools, run on throwaway repositories shaped like this one.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_upstreamTools.py
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
TOOLS = ROOT / "tools"

GIT_ENV = {
	"GIT_CONFIG_GLOBAL": os.devnull,
	"GIT_CONFIG_NOSYSTEM": "1",
	"GIT_AUTHOR_NAME": "Test",
	"GIT_AUTHOR_EMAIL": "test@example.invalid",
	"GIT_COMMITTER_NAME": "Test",
	"GIT_COMMITTER_EMAIL": "test@example.invalid",
}


class Repo:
	def __init__(self, path: Path):
		self.path = path

	def run(self, *args: str | Path, check: bool = True, **env: str) -> subprocess.CompletedProcess:
		done = subprocess.run(
			[str(a) for a in args],
			cwd=self.path,
			env={**os.environ, **GIT_ENV, **env},
			capture_output=True,
			text=True,
			check=False,
		)
		if check and done.returncode != 0:
			raise AssertionError(f"{args} exited {done.returncode}: {done.stdout}{done.stderr}")
		return done

	def git(self, *args: str) -> str:
		return self.run("git", *args).stdout.strip()

	def tool(self, name: str, *args: str, **env: str) -> subprocess.CompletedProcess:
		return self.run(TOOLS / name, *args, check=False, **env)

	def write(self, path: str, text: str) -> None:
		target = self.path / path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(text, encoding="utf-8")

	def read(self, path: str) -> str:
		return (self.path / path).read_text(encoding="utf-8")

	def commit(self, message: str, *paths: str) -> str:
		self.git("add", "--", *paths)
		self.git("commit", "-q", "-m", message)
		return self.git("rev-parse", "HEAD")

	def link(self, path: str, commit: str) -> None:
		"""Points a submodule link at commit, leaving its directory empty, as a clone does."""
		(self.path / path).mkdir(parents=True, exist_ok=True)
		self.git("update-index", "--add", "--cacheinfo", f"160000,{commit},{path}")

	def blob(self, ref: str, path: str) -> str:
		return self.git("rev-parse", f"{ref}:{path}")

	def onUpstream(self, change) -> None:
		"""Commits change(self) as a newer NVDA on upstream and merges it into main."""
		self.git("checkout", "-q", "upstream")
		change(self)
		self.git("commit", "-q", "-a", "-m", "Import a newer NVDA")
		self.git("checkout", "-q", "main")
		self.git("merge", "-q", "--no-edit", "upstream")


CLDR = "c" * 40
LOUIS = "1" * 40
ESPEAK = "e" * 40
SONIC = "5" * 40
EDITS_HEADER = "# Files that came from NVDA and that this tree changes or deletes.\n\n"
FINDER = "replacedModules: tuple[str, ...] = (\n\t{}\n)\n"
CLDR_NIX = 'fetchFromGitHub {{\n  repo = "nvda-cldr";\n  rev = "{}";\n}}\n'
ESPEAK_NIX = (
	'sonic = fetchFromGitHub {{\n  repo = "sonic";\n  rev = "{}";\n}};\n'
	'src = fetchFromGitHub {{\n  repo = "espeak-ng";\n  rev = "{}";\n}};\n'
)
FORKS = "tools/upstream-forks.txt"
LINUX_EDIT = 'import sys\n\nvalue = 1\nif sys.platform != "win32":\n\tvalue = 2\n'


def writeFinder(r: Repo, *modules: str) -> None:
	r.write("source/linuxPlatform/finder.py", FINDER.format("\n\t".join(f"{m!r}," for m in modules)))


def writeEdits(r: Repo, *paths: str) -> None:
	r.write("tools/upstream-edits.txt", EDITS_HEADER + "".join(f"{p}\n  Linux runtime: why\n" for p in paths))


def writeReason(r: Repo, *lines: str) -> None:
	r.write(
		"tools/upstream-edits.txt",
		EDITS_HEADER + "source/edited.py\n" + "".join(f"  {line}\n" for line in lines),
	)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
	"""An upstream branch holding a small NVDA tree, and main forked from it as this tree is.

	main edits one upstream file for Linux alone, replaces two modules through the finder, and mirrors
	nvda.pyw.
	"""
	r = Repo(tmp_path / "work")
	r.path.mkdir()
	r.git("init", "-q", "-b", "upstream")
	r.write("source/nvda.pyw", "launcher = True\n")
	r.write("source/tones.py", "def beep():\n\tpass\n")
	r.write("source/edited.py", "value = 1\n")
	r.write("source/watchdog.py", "def alive():\n\tpass\n")
	r.write("source/NVDAHelper/__init__.py", "def helper():\n\tpass\n")
	r.write("tests/unit/test_tones.py", "")
	r.link("include/nvda-cldr", CLDR)
	r.link("include/liblouis", LOUIS)
	r.link("include/espeak", ESPEAK)
	r.link("include/sonic", SONIC)
	r.commit("Import NVDA", "source", "tests")
	r.git("checkout", "-q", "-b", "main")
	r.write("source/edited.py", LINUX_EDIT)
	r.write("source/basedreader.py", "launcher = True\n")
	r.write("source/linuxPlatform/__init__.py", "")
	r.write("source/linuxPlatform/replacements/watchdog.py", "")
	writeFinder(r, "watchdog", "NVDAHelper")
	r.write("nix/nvda-cldr.nix", CLDR_NIX.format(CLDR))
	r.write("nix/espeak-ng.nix", ESPEAK_NIX.format(SONIC, ESPEAK))
	writeEdits(r, "source/edited.py")
	forks = [f"include/liblouis {LOUIS} 3.39.0"]
	for path in ("source/nvda.pyw", "source/watchdog.py", "source/NVDAHelper/__init__.py"):
		forks.append(f"{path} {r.blob('upstream', path)}")
	r.write(FORKS, "# forks\n\n" + "".join(f"{entry}\n  a reason\n" for entry in forks))
	r.commit("Fork", "source", "tools", "nix")
	return r


def check(r: Repo, louis: str = "3.39.0") -> subprocess.CompletedProcess:
	return r.tool("check-upstream-edits", BASEDREADER_LIBLOUIS_VERSION=louis)


def assertFails(r: Repo, *expected: str) -> None:
	done = check(r)
	assert done.returncode == 1, done.stdout + done.stderr
	for text in expected:
		assert text in done.stderr, done.stderr


def assertPasses(r: Repo) -> subprocess.CompletedProcess:
	done = check(r)
	assert done.returncode == 0, done.stdout + done.stderr
	assert done.stderr == ""
	return done


def test_treeOnRecordPasses(repo: Repo):
	done = assertPasses(repo)
	assert "1 upstream files differ, all listed; 3 forks match upstream" in done.stdout
	assert "liblouis: 3.39.0, as upstream pins" in done.stdout


def test_upstreamRefPrefersTheLocalBranch(repo: Repo):
	repo.git("update-ref", "refs/remotes/origin/upstream", "main")
	done = repo.tool("upstream-ref")
	assert done.returncode == 0, done.stderr
	assert done.stdout.strip() == "refs/heads/upstream"


def test_freshCloneUsesOriginUpstream(repo: Repo, tmp_path: Path):
	clone = Repo(tmp_path / "clone")
	repo.run("git", "clone", "-q", "--branch", "main", repo.path, clone.path)
	assert clone.tool("upstream-ref").stdout.strip() == "refs/remotes/origin/upstream"
	assertPasses(clone)
	assert clone.git("status", "--short") == ""


def test_remoteNamedUpstreamIsNeverTheBase(repo: Repo):
	# NVDA's own repository, added as a remote under the conventional name.
	repo.git("update-ref", "refs/remotes/upstream/HEAD", "main")
	repo.git("branch", "-q", "-m", "upstream", "pristine")
	for tool in ("upstream-ref", "check-upstream-edits"):
		done = repo.tool(tool)
		assert done.returncode != 0, tool
		assert "git fetch origin upstream:upstream" in done.stderr, tool


def test_unlistedEditFails(repo: Repo):
	repo.write("source/tones.py", "")
	assertFails(repo, "edited, deleted or changed in type but not listed:\nsource/tones.py")


def test_unlistedDeletionFails(repo: Repo):
	(repo.path / "source/tones.py").unlink()
	assertFails(repo, "edited, deleted or changed in type but not listed:\nsource/tones.py")


def test_entryForAnUntouchedFileFails(repo: Repo):
	repo.write("source/edited.py", "value = 1\n")
	assertFails(repo, "listed but identical to upstream:\nsource/edited.py")


def test_reasonWithoutAKindFails(repo: Repo):
	repo.write("tools/upstream-edits.txt", EDITS_HEADER + "source/edited.py\n  Linux: why\n")
	assertFails(repo, "reason not starting with one of import safety:, Linux runtime:")


def test_editWindowsRunsMustSaySo(repo: Repo):
	repo.write("source/edited.py", "value = 2\n")
	assertFails(
		repo, "Windows runs a change, but no line of the reason starts every platform:\nsource/edited.py"
	)
	writeReason(repo, "every platform: why")
	assertPasses(repo)


def test_editOnlyLinuxRunsMustNotClaimWindowsRunsIt(repo: Repo):
	writeReason(repo, "every platform: why")
	assertFails(
		repo, "a line starts every platform:, but Windows runs the same code as upstream:\nsource/edited.py"
	)


def test_reasonMayGiveSeveralKinds(repo: Repo):
	repo.write("source/edited.py", LINUX_EDIT + "other = 3\n")
	writeReason(repo, "Linux runtime: why", "and more of why", "every platform: why")
	assertPasses(repo)


@pytest.mark.parametrize(
	"lines",
	[
		["import safety: why. Linux runtime: why"],
		["import safety: why. Linux", "runtime: why"],
		["Linux runtime: why, and on every platform: why"],
	],
)
def test_kindInsideALineFails(repo: Repo, lines: list[str]):
	writeReason(repo, *lines)
	assertFails(repo, "a kind of edit inside a line rather than at the start of one:\nsource/edited.py")


UPSTREAM_MODULE = """import os
import winreg


def read(key: winreg.HKEYType, name):
	if not name:
		return None
	return winreg.QueryValue(key, name)
"""


def addUpstreamModule(r: Repo) -> None:
	r.write("source/registry.py", UPSTREAM_MODULE)
	r.git("add", "source/registry.py")


@pytest.mark.parametrize(
	"ours",
	[
		# winreg after the module's other imports, its annotation quoted, and standard library imports.
		"""import os
import ctypes.wintypes
import sys

if sys.platform == "win32":
	import winreg


def read(key: "winreg.HKEYType", name):
	if not name:
		return None
	return winreg.QueryValue(key, name)
""",
		# Linux's branch beside Windows', in each of the forms a test of sys.platform takes.
		"""import os
import sys
import winreg


def read(key: winreg.HKEYType, name):
	if not name and sys.platform != "win32":
		return ""
	elif not name:
		return None
	if sys.platform == "linux" or "win32" != sys.platform:
		return os.environ[name]
	return winreg.QueryValue(key, name) if sys.platform == "win32" else None
""",
	],
)
def test_editsKeptFromWindowsAreLinuxOnly(repo: Repo, ours: str):
	repo.onUpstream(addUpstreamModule)
	repo.write("source/registry.py", ours)
	writeEdits(repo, "source/edited.py", "source/registry.py")
	assertPasses(repo)


@pytest.mark.parametrize(
	"change",
	[
		("import winreg\n", 'import sys\n\nif sys.platform != "win32":\n\timport winreg\n'),
		("import winreg\n", "import winreg\nimport wx\n"),
		("\t\treturn None", "\t\treturn None if os.sep else 0"),
		("winreg.HKEYType", '"winreg.HKEY"'),
	],
)
def test_editsWindowsRunsAreSeen(repo: Repo, change: tuple[str, str]):
	repo.onUpstream(addUpstreamModule)
	repo.write("source/registry.py", UPSTREAM_MODULE.replace(*change))
	writeEdits(repo, "source/edited.py", "source/registry.py")
	assertFails(
		repo, "Windows runs a change, but no line of the reason starts every platform:\nsource/registry.py"
	)


def test_entryWithoutAReasonFails(repo: Repo):
	repo.write("tools/upstream-edits.txt", EDITS_HEADER + "source/edited.py\n")
	assertFails(repo, "no reason given for source/edited.py")


def test_fileTurnedIntoALinkFails(repo: Repo):
	(repo.path / "source/tones.py").unlink()
	(repo.path / "source/tones.py").symlink_to("watchdog.py")
	assertFails(repo, "edited, deleted or changed in type but not listed:\nsource/tones.py")


@pytest.mark.parametrize("path", ["source/locale.py", "source/appModules/thing.py", "tests/unit/test_new.py"])
def test_fileAddedAmongUpstreamsFailsUntilListed(repo: Repo, path: str):
	repo.write(path, "")
	assertFails(repo, f"added among upstream's files but not listed:\n{path}")
	repo.git("add", "--", path)
	assertFails(repo, f"added among upstream's files but not listed:\n{path}")
	writeEdits(repo, "source/edited.py", path)
	assertPasses(repo)


def test_fileNamedLikeTheLauncherIsNotOurs(repo: Repo):
	repo.write("source/basedreader.pyi", "")
	assertFails(repo, "added among upstream's files but not listed:\nsource/basedreader.pyi")


def test_fileAddedInOurOwnPlacesPasses(repo: Repo):
	repo.write("source/linuxPlatform/new.py", "")
	repo.git("add", "--", "source/linuxPlatform/new.py")
	repo.write("engine/new.rs", "")
	assertPasses(repo)


def test_forkWhoseUpstreamFileMovedFails(repo: Repo):
	old = repo.blob("upstream", "source/watchdog.py")
	repo.onUpstream(lambda r: r.write("source/watchdog.py", "def alive():\n\treturn True\n"))
	new = repo.blob("upstream", "source/watchdog.py")
	assertFails(
		repo,
		f"upstream changed source/watchdog.py: read git diff {old} {new}, carry what applies into"
		" source/linuxPlatform/replacements/watchdog.py",
	)
	repo.write(FORKS, repo.read(FORKS).replace(old, new))
	assertPasses(repo)


def test_launcherWhoseUpstreamMovedFails(repo: Repo):
	repo.onUpstream(lambda r: r.write("source/nvda.pyw", "launcher = False\n"))
	assertFails(repo, "upstream changed source/nvda.pyw", "carry what applies into source/basedreader.py")


def test_moduleNewlyReplacedWithoutAnEntryFails(repo: Repo):
	writeFinder(repo, "watchdog", "NVDAHelper", "tones")
	assertFails(repo, f"forked from upstream but not in {FORKS}:\nsource/tones.py")


def test_entryForAModuleNoLongerReplacedFails(repo: Repo):
	writeFinder(repo, "NVDAHelper")
	assertFails(repo, f"in {FORKS} but no longer forked:\nsource/watchdog.py")


def test_replacingAModuleUpstreamLacksFails(repo: Repo):
	writeFinder(repo, "watchdog", "NVDAHelper", "nowhere")
	assertFails(repo, "replaces nowhere, which upstream does not have")


def test_theLauncherIsAlwaysAFork(repo: Repo):
	repo.write(FORKS, repo.read(FORKS).replace("source/nvda.pyw", "# source/nvda.pyw"))
	assertFails(repo, f"forked from upstream but not in {FORKS}:\nsource/nvda.pyw")


def test_cldrPinOtherThanUpstreamsFails(repo: Repo):
	repo.write("nix/nvda-cldr.nix", CLDR_NIX.format("d" * 40))
	assertFails(repo, f"upstream pins nvda-cldr at {CLDR}, but nix/nvda-cldr.nix fetches {'d' * 40}")


def test_upstreamMovingASubmoduleMergesAndTheCldrCheckFails(repo: Repo):
	repo.onUpstream(lambda r: r.link("include/nvda-cldr", "e" * 40))
	assertFails(repo, f"upstream pins nvda-cldr at {'e' * 40}")


def test_espeakAndSonicPinsOtherThanUpstreamsFail(repo: Repo):
	repo.write("nix/espeak-ng.nix", ESPEAK_NIX.format("6" * 40, "f" * 40))
	assertFails(
		repo,
		f"upstream pins sonic at {SONIC}, but nix/espeak-ng.nix fetches {'6' * 40}",
		f"upstream pins espeak-ng at {ESPEAK}, but nix/espeak-ng.nix fetches {'f' * 40}",
	)


def test_upstreamMovingEspeakFailsUntilTheNixPinFollows(repo: Repo):
	repo.onUpstream(lambda r: r.link("include/espeak", "f" * 40))
	assertFails(repo, f"upstream pins espeak-ng at {'f' * 40}, but nix/espeak-ng.nix fetches {ESPEAK}")
	repo.write("nix/espeak-ng.nix", ESPEAK_NIX.format(SONIC, "f" * 40))
	assertPasses(repo)


def test_liblouisDifferencesAreReportedWithoutFailing(repo: Repo):
	done = check(repo, louis="3.40.0")
	assert done.returncode == 0, done.stderr
	assert "liblouis: nixpkgs supplies 3.40.0, upstream pins 3.39.0" in done.stdout
	repo.onUpstream(lambda r: r.link("include/liblouis", "2" * 40))
	done = assertPasses(repo)
	assert f"liblouis: upstream now pins {'2' * 40}, not {LOUIS}" in done.stdout


def test_thisTreeIsOnRecord():
	done = Repo(ROOT).tool("check-upstream-edits")
	assert done.returncode == 0, done.stdout + done.stderr


def test_thisTreeKeepsUpstreamsSubmoduleLinks():
	"""So that an upstream release moving a submodule merges instead of conflicting."""
	r = Repo(ROOT)
	base = r.tool("upstream-ref").stdout.strip()
	tree = r.git("ls-tree", "-r", base).splitlines()
	links = [line.split("\t")[1] for line in tree if line.startswith("160000")]
	assert links
	diff = r.run("git", "diff", "--quiet", base, "--", ".gitmodules", *links, check=False)
	assert diff.returncode == 0


@pytest.fixture
def nvda(tmp_path: Path) -> Repo:
	"""NVDA's repository: a release tag, and a beta branch already working towards the next."""
	r = Repo(tmp_path / "nvda")
	r.path.mkdir()
	r.git("init", "-q", "-b", "master")
	r.write("source/buildVersion.py", "version_year = 2027\nversion_major = 1\nversion_minor = 0\n")
	r.commit("Release", "source")
	r.git("tag", "-a", "-m", "release", "release-2027.1")
	r.git("checkout", "-q", "-b", "beta")
	r.write("source/buildVersion.py", "version_year = 2027\nversion_major = 2\nversion_minor = 1\n")
	r.commit("Next", "source")
	return r


def importUpstream(repo: Repo, nvda: Repo, name: str, parent: str) -> tuple[str, str]:
	"""Imports name, checks what was recorded, and gives the commit's subject and body."""
	before = repo.git("rev-parse", parent)
	done = repo.tool("import-upstream", name, f"file://{nvda.path}")
	assert done.returncode == 0, done.stdout + done.stderr
	imported = repo.git("rev-parse", "refs/heads/upstream")
	assert repo.git("rev-parse", f"{imported}^") == before
	assert repo.git("rev-parse", f"{imported}^{{tree}}") == nvda.git("rev-parse", f"{name}^{{tree}}")
	return repo.git("log", "-1", "--format=%s", imported), repo.git("log", "-1", "--format=%b", imported)


def test_importOfATagNamesTheRelease(repo: Repo, nvda: Repo):
	subject, body = importUpstream(repo, nvda, "release-2027.1", "refs/heads/upstream")
	assert subject == "Import NVDA 2027.1"
	assert nvda.git("rev-parse", "release-2027.1^{commit}") in body
	assert "tag release-2027.1" in body


def test_importOfABranchNamesTheBranchAndItsVersion(repo: Repo, nvda: Repo):
	subject, body = importUpstream(repo, nvda, "beta", "refs/heads/upstream")
	assert subject == "Import NVDA 2027.2.1 from branch beta"
	assert nvda.git("rev-parse", "beta") in body
	assert "branch beta" in body and "tag" not in body


def test_importInAFreshCloneBuildsOnOriginUpstream(repo: Repo, nvda: Repo, tmp_path: Path):
	clone = Repo(tmp_path / "clone")
	repo.run("git", "clone", "-q", "--branch", "main", repo.path, clone.path)
	importUpstream(clone, nvda, "release-2027.1", "refs/remotes/origin/upstream")
