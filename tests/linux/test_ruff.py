"""Our Python, and our edits to upstream's, pass upstream's own ruff configuration and release.

Upstream's pre-commit runs ruff's linter and formatter, so a hunk of ours that they would rewrite
or reject turns into a conflict for whoever merges the next upstream release. Releases of ruff
disagree on what they report, so only the one upstream's pyproject.toml pins answers that, and
any other ruff fails these tests rather than passing them on its own terms. An edited upstream
file is held to what ruff says about upstream's own version of it, not to a clean result, because
upstream's files are not all clean.

nix develop . -c tests/linux/rig python -m pytest tests/linux/test_ruff.py
"""

import functools
import json
import re
import shutil
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER = ROOT / "tools" / "upstream-edits.txt"
#: Files without a .py extension are checked only when named.
OURS = (
	"source/linuxPlatform",
	"source/basedreader.py",
	"tests/linux",
	"tools/build-artifacts",
	"tools/check-upstream-edits",
)
RUFF = shutil.which("ruff")


def pinnedVersion() -> str:
	with open(ROOT / "pyproject.toml", "rb") as f:
		lint = tomllib.load(f)["dependency-groups"]["lint"]
	return next(dep.removeprefix("ruff==") for dep in lint if dep.startswith("ruff=="))


@pytest.fixture(autouse=True, scope="module")
def ruffIsUpstreams() -> None:
	if RUFF is None:
		pytest.fail("ruff is not on PATH; run the tests in the dev shell", pytrace=False)
	shown = subprocess.run([RUFF, "--version"], capture_output=True, text=True, check=True).stdout
	found = re.fullmatch(r"ruff (\S+)\s*", shown)
	running = found[1] if found else shown.strip()
	pinned = pinnedVersion()
	if running != pinned:
		pytest.fail(
			f"ruff is {running}, upstream's pyproject.toml pins {pinned}; move flake.nix's nixpkgs-ruff",
			pytrace=False,
		)


def ruff(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
	return subprocess.run(
		[RUFF, *args, "--no-cache"],
		input=stdin,
		capture_output=True,
		text=True,
		cwd=ROOT,
		check=False,
	)


def editedUpstreamPythonFiles() -> list[str]:
	listed = [
		line.strip()
		for line in LEDGER.read_text(encoding="utf-8").splitlines()
		if line[:1] not in ("", "#", " ")
	]
	return [path for path in listed if path.endswith(".py") and (ROOT / path).is_file()]


@functools.cache
def upstreamBase() -> str:
	found = subprocess.run(
		[ROOT / "tools" / "upstream-ref"],
		capture_output=True,
		text=True,
		cwd=ROOT,
		check=False,
	)
	if found.returncode != 0:
		pytest.fail(found.stderr.strip(), pytrace=False)
	return found.stdout.strip()


def upstreamVersion(path: str) -> str | None:
	"""Upstream's text of path, or None for a file this tree adds among upstream's."""
	shown = subprocess.run(
		["git", "show", f"{upstreamBase()}:{path}"],
		capture_output=True,
		text=True,
		cwd=ROOT,
		check=False,
	)
	return shown.stdout if shown.returncode == 0 else None


def findings(path: str, source: str | None) -> Counter:
	if source is None:
		return Counter()
	done = ruff("check", "--output-format", "json", "--stdin-filename", path, stdin=source)
	return Counter(f"{f['code']}: {f['message']}" for f in json.loads(done.stdout))


def isFormatted(path: str, source: str | None) -> bool:
	if source is None:
		return True
	return ruff("format", "--check", "--stdin-filename", path, stdin=source).returncode == 0


def test_ourPythonPassesRuff():
	checked = ruff("check", "--output-format", "concise", *OURS)
	assert checked.returncode == 0, checked.stdout + checked.stderr
	formatted = ruff("format", "--check", *OURS)
	assert formatted.returncode == 0, formatted.stdout + formatted.stderr


def test_upstreamEditsAddNoRuffFindings():
	added = {}
	for path in editedUpstreamPythonFiles():
		ours = (ROOT / path).read_text(encoding="utf-8")
		extra = findings(path, ours) - findings(path, upstreamVersion(path))
		if extra:
			added[path] = sorted(extra.elements())
	assert not added, json.dumps(added, indent=1)


def test_upstreamEditsKeepFormatting():
	reformatted = [
		path
		for path in editedUpstreamPythonFiles()
		if isFormatted(path, upstreamVersion(path))
		and not isFormatted(path, (ROOT / path).read_text(encoding="utf-8"))
	]
	assert not reformatted, f"ruff format would rewrite our hunks in: {reformatted}"
