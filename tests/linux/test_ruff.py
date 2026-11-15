"""Our Python, and our edits to upstream's, pass upstream's own ruff configuration.

Upstream's pre-commit runs ruff's linter and formatter, so a hunk of ours that they would rewrite
or reject turns into a conflict for whoever merges the next upstream release. An edited upstream
file is held to what ruff says about upstream's own version of it, not to a clean result, because
upstream's files are not all clean under every ruff release.

	nix develop . -c python3 -m pytest -p no:cacheprovider tests/linux/test_ruff.py
"""

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER = ROOT / "tools" / "upstream-edits.txt"
OURS = ("source/linuxPlatform", "tests/linux")
RUFF = shutil.which("ruff")

pytestmark = pytest.mark.skipif(
	RUFF is None, reason="ruff is not on PATH; the dev shell does not provide it yet"
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


def upstreamVersion(path: str) -> str:
	return subprocess.run(
		["git", "show", f"upstream:{path}"],
		capture_output=True,
		text=True,
		cwd=ROOT,
		check=True,
	).stdout


def findings(path: str, source: str) -> Counter:
	done = ruff("check", "--output-format", "json", "--stdin-filename", path, stdin=source)
	return Counter(f"{f['code']}: {f['message']}" for f in json.loads(done.stdout))


def isFormatted(path: str, source: str) -> bool:
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
