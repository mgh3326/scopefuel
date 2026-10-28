"""Wrap tests/test-ci-any-ref.sh so the structural + dry-run harness runs under pytest."""

import subprocess
from pathlib import Path


def test_ci_any_ref():
    script = Path(__file__).parent / "test-ci-any-ref.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=script.parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
