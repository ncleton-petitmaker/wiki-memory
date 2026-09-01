from __future__ import annotations

import sys
from pathlib import Path


def convert(source: str, output: Path) -> None:
    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.document.export_to_markdown(), encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m wiki_memory.docling_bridge SOURCE OUTPUT.md")
    convert(sys.argv[1], Path(sys.argv[2]))


if __name__ == "__main__":
    main()
