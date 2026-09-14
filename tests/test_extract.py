"""Extraction unit tests — implemented in the next building step."""

from src.check import parse_args


def test_cli_defaults():
    args = parse_args([])
    assert args.scope == "diff"
