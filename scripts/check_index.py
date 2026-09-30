"""Fail if a workspace package or top-level dir is missing from INDEX.md (keeps the map honest)."""

from __future__ import annotations

import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
index = (root / "INDEX.md").read_text()
missing = [
    str(p.relative_to(root))
    for pat in ("libs/*", "services/*", "apps/*")
    for p in sorted(root.glob(pat))
    if p.is_dir() and str(p.relative_to(root)) not in index
]
if missing:
    print("INDEX.md is missing:", ", ".join(missing))
    sys.exit(1)
