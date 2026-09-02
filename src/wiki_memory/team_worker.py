from __future__ import annotations

import argparse
import json
import signal
import time

from .team_server import repository_from_environment


def main() -> None:
    parser = argparse.ArgumentParser(prog="wiki-memory-team-worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--batch", type=int, default=100)
    args = parser.parse_args()
    repository = repository_from_environment()
    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        result = repository.run_jobs_once(args.batch)
        print(json.dumps(result), flush=True)
        if args.once:
            break
        if result["claimed"] == 0:
            time.sleep(max(args.interval, 0.1))


if __name__ == "__main__":
    main()
