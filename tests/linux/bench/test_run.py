"""Which bench binary tests/linux/bench/run uses, checked with zero runs so that no Firefox starts.

nix develop . -c tests/linux/rig python -m pytest tests/linux/bench/test_run.py
"""

import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
ENGINE_BUILD = ROOT / "engine" / "target" / "release" / "linda-bench"


def runBench(tmp_path: Path, **env: str | None) -> subprocess.CompletedProcess:
	environ = {k: v for k, v in {**os.environ, **env}.items() if v is not None}
	return subprocess.run(
		[HERE / "run", "normal", tmp_path / "out", "0"],
		env=environ,
		capture_output=True,
		text=True,
		check=False,
	)


def chosen(done: subprocess.CompletedProcess) -> str:
	return done.stdout + done.stderr


def fakeBench(tmp_path: Path, name: str) -> Path:
	path = tmp_path / name
	path.write_text("#!/bin/sh\n", encoding="ascii")
	path.chmod(0o755)
	return path


def test_benchBinOverridesEverything(tmp_path: Path):
	given = fakeBench(tmp_path, "given")
	built = fakeBench(tmp_path, "built")
	done = runBench(tmp_path, BENCH_BIN=str(given), LINDA_BENCH_BIN=str(built))
	assert done.returncode == 0, chosen(done)
	assert f", {given}, " in done.stdout


def test_theFlakesBenchIsUsedOnceBuilt(tmp_path: Path):
	built = fakeBench(tmp_path, "built")
	done = runBench(tmp_path, BENCH_BIN=None, LINDA_BENCH_BIN=str(built))
	assert done.returncode == 0, chosen(done)
	assert f", {built}, " in done.stdout


def test_cargosBuildIsTheFallback(tmp_path: Path):
	done = runBench(tmp_path, BENCH_BIN=None, LINDA_BENCH_BIN=str(tmp_path / "unbuilt"))
	assert str(ENGINE_BUILD) in chosen(done)
	if not ENGINE_BUILD.exists():
		assert done.returncode == 2
		assert f"nix build {ROOT}#linda-bench" in done.stderr
