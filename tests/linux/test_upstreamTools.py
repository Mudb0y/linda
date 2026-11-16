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

	def run(self, *args: str | Path, check: bool = True) -> subprocess.CompletedProcess:
		done = subprocess.run(
			[str(a) for a in args],
			cwd=self.path,
			env={**os.environ, **GIT_ENV},
			capture_output=True,
			text=True,
			check=False,
		)
		if check and done.returncode != 0:
			raise AssertionError(f"{args} exited {done.returncode}: {done.stdout}{done.stderr}")
		return done

	def git(self, *args: str) -> str:
		return self.run("git", *args).stdout.strip()

	def tool(self, name: str, *args: str) -> subprocess.CompletedProcess:
		return self.run(TOOLS / name, *args, check=False)

	def write(self, path: str, text: str) -> None:
		target = self.path / path
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(text, encoding="utf-8")

	def commit(self, message: str, *paths: str) -> str:
		self.git("add", "--", *paths)
		self.git("commit", "-q", "-m", message)
		return self.git("rev-parse", "HEAD")


EDITS_HEADER = "# Files that came from NVDA and that this tree changes or deletes.\n\n"


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
	"""An upstream branch holding a small NVDA tree, and main forked from it with one edit."""
	r = Repo(tmp_path / "work")
	r.path.mkdir()
	r.git("init", "-q", "-b", "upstream")
	r.write("source/nvda.pyw", "launcher = True\n")
	r.write("source/tones.py", "def beep():\n\tpass\n")
	r.write("source/edited.py", "value = 1\n")
	r.commit("Import NVDA", "source")
	r.git("checkout", "-q", "-b", "main")
	r.write("source/edited.py", "value = 2\n")
	r.write("source/linuxPlatform/__init__.py", "")
	r.write("tools/upstream-edits.txt", EDITS_HEADER + "source/edited.py\n  a reason\n")
	r.commit("Fork", "source", "tools")
	return r


def test_upstreamRefPrefersTheLocalBranch(repo: Repo):
	repo.git("update-ref", "refs/remotes/origin/upstream", "main")
	done = repo.tool("upstream-ref")
	assert done.returncode == 0, done.stderr
	assert done.stdout.strip() == "refs/heads/upstream"


def test_freshCloneUsesOriginUpstream(repo: Repo, tmp_path: Path):
	clone = Repo(tmp_path / "clone")
	repo.run("git", "clone", "-q", "--branch", "main", repo.path, clone.path)
	assert clone.tool("upstream-ref").stdout.strip() == "refs/remotes/origin/upstream"
	checked = clone.tool("check-upstream-edits")
	assert checked.returncode == 0, checked.stdout + checked.stderr


def test_remoteNamedUpstreamIsNeverTheBase(repo: Repo):
	# NVDA's own repository, added as a remote under the conventional name.
	repo.git("update-ref", "refs/remotes/upstream/HEAD", "main")
	repo.git("branch", "-q", "-m", "upstream", "pristine")
	for tool in ("upstream-ref", "check-upstream-edits"):
		done = repo.tool(tool)
		assert done.returncode != 0, tool
		assert "git fetch origin upstream:upstream" in done.stderr, tool


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
