#!/usr/bin/env python3
"""Synthetic PostgreSQL Team search benchmark for the one-million-fragment gate."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import platform
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.events import EventActor, MemoryEvent, PluginRef
from wiki_memory.team import normalize_acl
from wiki_memory.team_server import database_dsn_from_environment
from wiki_memory.team_repository import PostgresTeamRepository


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("Cannot compute a percentile without samples.")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the synthetic one-million-fragment Team search benchmark")
    parser.add_argument("--fragments", type=int, default=1_000_000)
    parser.add_argument("--warmup", type=int, default=1_000)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--sample-every", type=int, default=100)
    parser.add_argument(
        "--workers",
        type=int,
        default=min(16, max(os.cpu_count() or 1, 1)),
        help="concurrent synthetic connector streams (default: up to 16)",
    )
    parser.add_argument("--members", type=int, default=0, help="distinct group-authorized member searches after indexing")
    parser.add_argument(
        "--member-workers",
        type=int,
        default=32,
        help="maximum concurrent member search requests",
    )
    parser.add_argument("--report", type=Path, help="write the aggregate JSON report to this new file")
    parser.add_argument("--assert-target", action="store_true", help="require the real 1,000,000-fragment <2s p95 gate")
    parser.add_argument(
        "--assert-operational-scale",
        action="store_true",
        help="require 100 connector streams and 500 distinct authorized members with no failed member search",
    )
    args = parser.parse_args()
    if (
        args.fragments < 1
        or args.warmup < 0
        or args.queries < 1
        or args.sample_every < 1
        or args.workers < 1
        or args.members < 0
        or args.member_workers < 1
    ):
        parser.error("fragments, queries, sample-every, workers, and member-workers must be positive; members/warmup cannot be negative")
    if args.assert_target and args.fragments != 1_000_000:
        parser.error("--assert-target requires --fragments 1000000; smaller runs are smoke tests")

    repository = PostgresTeamRepository(database_dsn_from_environment())
    repository.initialize()
    run_id = uuid.uuid4().hex
    space_id = f"benchmark-{run_id[:12]}"
    owner = "benchmark-service"
    member_group = f"benchmark-members-{run_id[:12]}"
    acl = normalize_acl(
        {"groups": [member_group], "audience": "explicit"} if args.members else {},
        owner=owner,
        space_id=space_id,
    )
    append_samples: list[float] = []

    def append_fragment(index: int) -> tuple[int, float]:
        token = f"wiki-memory-team-performance-token-{index:08d}"
        event = MemoryEvent(
            event_type="source.captured",
            stream_id=f"benchmark:{run_id}:{index}",
            idempotency_key=f"benchmark:{run_id}:{index}",
            actor=EventActor("system", owner),
            plugin=PluginRef("team-benchmark", "1.0.0"),
            scope="team",
            space_id=space_id,
            acl=acl,
            payload={"body": f"Synthetic Team fragment {index}. {token}. Durable memory evidence."},
        )
        started = time.perf_counter()
        repository.append(event, expected_stream_version=0)
        return index, time.perf_counter() - started

    # Each task is a distinct source stream, just like independently running
    # connectors.  This exercises the public append transaction and advisory
    # locks under concurrent load; it deliberately does not bulk-COPY rows or
    # bypass canonical event validation merely to make a benchmark look good.
    total_fragments = args.fragments + args.warmup
    batch_size = max(args.workers * 64, 1024)
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="team-benchmark") as executor:
        for batch_start in range(0, total_fragments, batch_size):
            batch_end = min(batch_start + batch_size, total_fragments)
            for index, elapsed in executor.map(append_fragment, range(batch_start, batch_end)):
                if index >= args.warmup and (
                    (index - args.warmup) % args.sample_every == 0 or index == total_fragments - 1
                ):
                    append_samples.append(elapsed)

    rebuild_started = time.perf_counter()
    rebuild = repository.rebuild_search_projection()
    rebuild_seconds = time.perf_counter() - rebuild_started
    token = f"wiki-memory-team-performance-token-{args.fragments + args.warmup - 1:08d}"
    query_samples: list[float] = []
    result_count = 0
    for _ in range(args.queries):
        started = time.perf_counter()
        result = repository.search(token, 10, {space_id}, False, principal_id=owner, groups=set())
        query_samples.append(time.perf_counter() - started)
        result_count = len(result)
    query_p95 = percentile(query_samples, 0.95)
    member_samples: list[float] = []
    member_failures = 0

    def member_search(index: int) -> tuple[float, bool]:
        started = time.perf_counter()
        result = repository.search(
            token,
            10,
            {space_id},
            False,
            principal_id=f"benchmark-member-{index:04d}",
            groups={member_group},
        )
        return time.perf_counter() - started, bool(result)

    if args.members:
        with ThreadPoolExecutor(
            max_workers=min(args.members, args.member_workers), thread_name_prefix="team-members"
        ) as executor:
            for elapsed, authorized in executor.map(member_search, range(args.members)):
                member_samples.append(elapsed)
                member_failures += int(not authorized)
    full_scale = args.fragments == 1_000_000
    operational_scale = args.workers >= 100 and args.members >= 500 and member_failures == 0
    report = {
        "ok": result_count > 0,
        "corpus": {
            "fragments": args.fragments,
            "warmup": args.warmup,
            "appendSamples": len(append_samples),
            "concurrentConnectorStreams": args.workers,
        },
        "environment": {"platform": platform.platform(), "python": sys.version.split()[0], "cpuCount": os.cpu_count()},
        "append": {"p95Milliseconds": round(percentile(append_samples, 0.95) * 1000, 3)},
        "projectionRebuildSeconds": round(rebuild_seconds, 3),
        "search": {
            "p50Milliseconds": round(percentile(query_samples, 0.50) * 1000, 3),
            "p95Milliseconds": round(query_p95 * 1000, 3),
            "samples": len(query_samples),
            "resultCount": result_count,
            "rebuildDocuments": rebuild["documents"],
        },
        "members": {
            "distinctAuthorizedMembers": args.members,
            "concurrentSearchRequests": min(args.members, args.member_workers),
            "failedSearches": member_failures,
            "p95Milliseconds": round(percentile(member_samples, 0.95) * 1000, 3) if member_samples else None,
        },
        "targets": {
            "searchP95Under2sAt1m": query_p95 < 2.0 if full_scale else None,
            "fullScaleExecuted": full_scale,
            "operationalScale100Connectors500Members": operational_scale if args.assert_operational_scale else None,
        },
    }
    serialized = json.dumps(report, sort_keys=True)
    if args.report is not None:
        report_path = args.report.expanduser().resolve()
        if report_path.exists():
            raise RuntimeError("Team benchmark --report must not overwrite an existing file.")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    target_ok = not args.assert_target or bool(report["targets"]["searchP95Under2sAt1m"])
    scale_ok = not args.assert_operational_scale or operational_scale
    return 0 if target_ok and scale_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
