"""Print the CHANGELOG.md section for a release tag.

Used by the release workflow so the published GitHub release body is the same
text as the repository file, rather than a second copy that can drift.

Exit codes: 0 printed, 1 no section for that tag or the section is empty.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section_for(version: str) -> str | None:
    """Return the body under `## [<version>]`, or None if there is none."""
    text = CHANGELOG.read_text(encoding="utf-8")

    match = re.search(
        r"^## \[" + re.escape(version) + r"\].*?$(.*?)(?=^## \[|\Z)",
        text,
        re.S | re.M,
    )

    if match is None:
        return None

    return match.group(1).strip() or None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <tag>", file=sys.stderr)
        return 1

    tag = argv[1]
    version = tag[1:] if tag.startswith("v") else tag

    body = section_for(version)
    if body is None:
        print(
            f"CHANGELOG.md has no non-empty section for {tag}; "
            f"expected a heading like '## [{version}]'",
            file=sys.stderr,
        )
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
