import sys


def test_ci_failure_probe():
    assert sys.version_info[:2] != (3, 11), "intentional CI failure probe"
