#!/usr/bin/env python3
"""Reproducible synthetic performance harness for Wiki Memory release gates.

It measures the actual public capture and query paths. It intentionally never
uses customer data or claims a target was met at a smaller corpus size.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.capture import capture_item
from wiki_memory.config import MemoryError
from wiki_memory.layout import init_memory
from wiki_memory.search import configure_index, query_memory


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("Cannot compute a percentile without samples.")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def benchmark(root: Path, *, documents: int, warmup: int, queries: int, use_qmd: bool) -> dict[str, object]:
    init_memory(
        root,
        {
            "name": "Wiki Memory synthetic performance benchmark",
            "language": "en",
            "sync_enabled": False,
            "vaults": [{"slug": "knowledge", "title": "Knowledge", "purpose": "Synthetic performance fixture"}],
        },
    )
    capture_seconds: list[float] = []
    for index in range(documents + warmup):
        token = f"wiki-memory-performance-token-{index:08d}"
        started = time.perf_counter()
        capture_item(
            root,
            "knowledge",
            source_type="benchmark",
            text=f"Synthetic benchmark document {index}. {token}. Shared vocabulary: durable memory evidence.",
            title=f"Synthetic benchmark {index}",
            connector="benchmark",
        )
        if index >= warmup:
            capture_seconds.append(time.perf_counter() - started)

    index_result: dict[str, object] | None = None
    previous_qmd_config = os.environ.get("WIKI_MEMORY_QMD_CONFIG_DIR")
    previous_qmd_cache = os.environ.get("WIKI_MEMORY_QMD_CACHE_DIR")
    if use_qmd:
        # Keep this run's transient collection registry and SQLite index next
        # to the synthetic corpus, never in the user's regular QMD runtime.
        os.environ["WIKI_MEMORY_QMD_CONFIG_DIR"] = str(root.parent / ".qmd-benchmark-config")
        os.environ["WIKI_MEMORY_QMD_CACHE_DIR"] = str(root.parent / ".qmd-benchmark-cache")
    try:
        if use_qmd:
            started = time.perf_counter()
            index_result = dict(configure_index(root, embed=False))
            index_result["elapsedSeconds"] = round(time.perf_counter() - started, 6)

        query_token = f"wiki-memory-performance-token-{documents + warmup - 1:08d}"
        query_seconds: list[float] = []
        query_engines: set[str] = set()
        result_count = 0
        for _ in range(queries):
            started = time.perf_counter()
            result = query_memory(root, query_token, limit=10)
            query_seconds.append(time.perf_counter() - started)
            query_engines.add(str(result["engine"]))
            result_count = len(result["results"])
    finally:
        if previous_qmd_config is None:
            os.environ.pop("WIKI_MEMORY_QMD_CONFIG_DIR", None)
        else:
            os.environ["WIKI_MEMORY_QMD_CONFIG_DIR"] = previous_qmd_config
        if previous_qmd_cache is None:
            os.environ.pop("WIKI_MEMORY_QMD_CACHE_DIR", None)
        else:
            os.environ["WIKI_MEMORY_QMD_CACHE_DIR"] = previous_qmd_cache

    capture_p95 = percentile(capture_seconds, 0.95)
    query_p95 = percentile(query_seconds, 0.95)
    full_scale = documents == 100_000
    return {
        "ok": result_count > 0,
        "corpus": {"documents": documents, "warmup": warmup, "bytesPerDocument": "approximately 120"},
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cpuCount": os.cpu_count(),
            "searchRequested": "qmd" if use_qmd else "text-fallback-or-configured-qmd",
        },
        "capture": {
            "samples": len(capture_seconds),
            "p50Milliseconds": round(percentile(capture_seconds, 0.50) * 1000, 3),
            "p95Milliseconds": round(capture_p95 * 1000, 3),
        },
        "search": {
            "samples": len(query_seconds),
            "p50Milliseconds": round(percentile(query_seconds, 0.50) * 1000, 3),
            "p95Milliseconds": round(query_p95 * 1000, 3),
            "engines": sorted(query_engines),
            "resultCount": result_count,
        },
        "qmdIndex": index_result,
        "targets": {
            "captureP95Under500ms": capture_p95 < 0.5,
            "searchP95Under1sAt100k": query_p95 < 1.0 if full_scale else None,
            "fullScaleExecuted": full_scale,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run synthetic Wiki Memory local performance measurements")
    parser.add_argument("--documents", type=int, default=100_000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--root", type=Path, help="empty directory to preserve the synthetic corpus for inspection")
    parser.add_argument("--report", type=Path, help="write the aggregate JSON report to this new file")
    parser.add_argument("--qmd", action="store_true", help="build and measure a QMD lexical index")
    parser.add_argument("--assert-targets", action="store_true", help="fail unless the actual 100k target run meets both local gates")
    args = parser.parse_args()
    if args.documents < 1 or args.warmup < 0 or args.queries < 1:
        parser.error("--documents and --queries must be positive; --warmup cannot be negative")
    if args.assert_targets and args.documents != 100_000:
        parser.error("--assert-targets requires --documents 100000; smaller runs are smoke tests, not release evidence")
    if args.root is not None:
        root = args.root.expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            raise MemoryError("Benchmark --root must be empty.")
        report = benchmark(root, documents=args.documents, warmup=args.warmup, queries=args.queries, use_qmd=args.qmd)
    else:
        with tempfile.TemporaryDirectory(prefix="wiki-memory-benchmark-") as temporary:
            report = benchmark(Path(temporary) / "memory", documents=args.documents, warmup=args.warmup, queries=args.queries, use_qmd=args.qmd)
    serialized = json.dumps(report, sort_keys=True)
    if args.report is not None:
        report_path = args.report.expanduser().resolve()
        if report_path.exists():
            raise MemoryError("Benchmark --report must not overwrite an existing file.")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    targets = report["targets"]
    return 0 if not args.assert_targets or (targets["captureP95Under500ms"] and targets["searchP95Under1sAt100k"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
