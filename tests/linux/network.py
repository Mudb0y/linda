"""Lets a test say that it needs the internet, which tests/linux/rig gives only when its caller sets
LINDA_RIG_NET=1. Elsewhere such a test is skipped, and conftest.py counts that skip as
expected: the package's checks build without a network, and the suite is normally run without one.

LINDA_RIG_NET=1 nix develop . -c tests/linux/rig python -m pytest tests/linux/test_rig.py
"""

import os

import pytest

#: Set in the rig's caller to give the rig the internet; the rig passes it in, so it holds inside.
VARIABLE = "LINDA_RIG_NET"

#: Why a test that needs the internet was skipped; conftest.py recognises the skip by it.
SKIP_REASON = f"needs the internet, which the rig gives only when started with {VARIABLE}=1"


def isNetworked() -> bool:
	"""Whether the rig this process runs in was started with the internet."""
	return os.environ.get(VARIABLE) == "1"


def requireNetwork() -> None:
	"""Skips the calling test unless the rig it runs in has the internet."""
	if not isNetworked():
		pytest.skip(SKIP_REASON)
