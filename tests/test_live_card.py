"""Exercise custom card lifecycle with Node's dependency-free browser doubles."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_live_card_browser_lifecycle():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the standalone card lifecycle checks")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_suffix(".mjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
