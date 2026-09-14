"""Advisory external-link + anchor checker for Antora AsciiDoc docs.

Initiation skeleton only — full extractor/resolver/checker lands in the
next building step. This keeps the CLI contract stable from the first commit.
"""

from __future__ import annotations

import argparse
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", default="diff", choices=["diff", "full"])
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--paths", default="documentation/**/*.adoc")
    parser.add_argument("--attributes-file", default="")
    parser.add_argument("--allowlist", default="")
    parser.add_argument("--timeout", default="15")
    parser.add_argument("--concurrency", default="8")
    parser.add_argument("--fail-on-error", default="false")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"spring-docs-link-check skeleton: scope={args.scope} (not implemented yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
