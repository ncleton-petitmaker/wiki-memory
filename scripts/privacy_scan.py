#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.quality import scan_privacy  # noqa: E402


result = scan_privacy(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve())
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["ok"] else 1)
