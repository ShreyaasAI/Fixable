"""CI gate (m8-contract §13 A5): gVisor tests ran and passed; nothing skipped for lack of runsc or
Docker. Usage: uv run --no-project python ops/ci_check_junit.py <junit.xml>"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

BAD_SKIPS = ("gVisor unavailable", "Docker unavailable")


def main(path: str) -> int:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    errors: list[str] = []
    gvisor = [c for c in cases if c.get("classname", "").startswith("tests.test_m8_gvisor")]
    if not gvisor:
        errors.append("no tests.test_m8_gvisor cases in the report")
    for case in cases:
        name = f"{case.get('classname')}::{case.get('name')}"
        for skip in case.iter("skipped"):
            msg = skip.get("message", "")
            if case in gvisor or any(b in msg for b in BAD_SKIPS):
                errors.append(f"skipped: {name}: {msg[:200]}")
        if case in gvisor and (case.find("failure") is not None or case.find("error") is not None):
            errors.append(f"failed: {name}")
    print(f"{len(cases)} cases, {len(gvisor)} in test_m8_gvisor")
    for line in errors:
        print(line, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
