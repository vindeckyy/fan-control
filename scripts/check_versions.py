#!/usr/bin/env python3
"""Check that release versions are synchronized across packaging specifications."""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CHECKS = {
    "Daemon": (ROOT / "fan_policy.py", r'^APP_VERSION = "([^"]+)"'),
    "UI package": (ROOT / "ui" / "package.json", r'"version":\s*"([^"]+)"'),
    "PKGBUILD": (ROOT / "packaging" / "PKGBUILD", r"^pkgver=([^\s]+)"),
    "RPM spec": (ROOT / "packaging" / "fan-control.spec", r"^Version:\s*([^\s]+)"),
    "Debian changelog": (ROOT / "packaging" / "debian" / "changelog", r"^fan-control \((?:\d+:)?([^)-]+)"),
    "AppStream metainfo": (
        ROOT / "packaging" / "org.community.FanControl.metainfo.xml",
        r'<release\s+version="([^"]+)"',
    ),
}


def main():
    versions = {}
    errors = []

    for name, (path, pattern) in CHECKS.items():
        if not path.is_file():
            errors.append(f"Missing file for {name}: {path}")
            continue
        content = path.read_text(encoding="utf-8")
        match = re.search(pattern, content, re.MULTILINE)
        if not match:
            errors.append(f"Could not find version pattern in {name} ({path})")
            continue
        versions[name] = match.group(1).strip()

    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        sys.exit(1)

    distinct = set(versions.values())
    if len(distinct) != 1:
        print("error: version mismatch detected across packaging files:", file=sys.stderr)
        for name, ver in sorted(versions.items()):
            print(f"  {name:20s}: {ver}", file=sys.stderr)
        sys.exit(1)

    version = next(iter(distinct))
    print(f"Versions synchronized at {version}:")
    for name in sorted(versions.keys()):
        print(f"  {name:20s}: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
