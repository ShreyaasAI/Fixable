"""CI gate (m8-contract §13 A5, m9-contract T9): gVisor tests ran and passed; nothing skipped for
lack of runsc or Docker; test_m9_workspace_owner executed (no failures); no test_m9_pkgcache_docker
case skipped. Usage: python3 ops/ci_check_junit.py <junit.xml>"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

BAD_SKIPS = ("gVisor unavailable", "Docker unavailable")


def _in_gvisor(classname: str) -> bool:
    return classname == "tests.test_m8_gvisor" or classname.startswith("tests.test_m8_gvisor.")


def _in_module(classname: str, module: str) -> bool:
    return classname == module or classname.startswith(module + ".")


def _failed(case: ET.Element) -> bool:
    return case.find("failure") is not None or case.find("error") is not None


def main(path: str) -> int:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    errors: list[str] = []
    gvisor = [c for c in cases if _in_gvisor(c.get("classname", ""))]
    ran = [c for c in gvisor if c.find("skipped") is None]
    if not ran:
        errors.append("no tests.test_m8_gvisor case ran")
    for case in cases:
        name = f"{case.get('classname')}::{case.get('name')}"
        for skip in case.iter("skipped"):
            # A gvisor test may skip only for the inverse case ("runsc is installed ...").
            msg = skip.get("message", "")
            if any(b in msg for b in BAD_SKIPS):
                errors.append(f"skipped: {name}: {msg[:200]}")
        if case in gvisor and (case.find("failure") is not None or case.find("error") is not None):
            errors.append(f"failed: {name}")
    owner = [
        c for c in cases if _in_module(c.get("classname", ""), "tests.test_m9_workspace_owner")
    ]
    owner_ran = [c for c in owner if c.find("skipped") is None]
    if not owner_ran:
        errors.append("no tests.test_m9_workspace_owner case executed (test_m9_workspace_owner)")
    errors += [f"failed: {c.get('classname')}::{c.get('name')}" for c in owner_ran if _failed(c)]
    pkg = [c for c in cases if _in_module(c.get("classname", ""), "tests.test_m9_pkgcache_docker")]
    if not pkg:
        errors.append("no tests.test_m9_pkgcache_docker case ran (test_m9_pkgcache_docker)")
    for c in pkg:
        if c.find("skipped") is not None:
            errors.append(f"skipped: test_m9_pkgcache_docker: {c.get('name')}")
    print(
        f"{len(cases)} cases, {len(ran)}/{len(gvisor)} test_m8_gvisor ran, "
        f"{len(owner_ran)}/{len(owner)} test_m9_workspace_owner ran, "
        f"{len(pkg)} test_m9_pkgcache_docker"
    )
    for line in errors:
        print(line, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
