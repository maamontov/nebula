"""Frozen entrypoint for the embedded Nebula API and pipeline worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading


def monitor_parent_pipe() -> None:
    """Exit when the Tauri parent closes the inherited stdin pipe."""

    try:
        sys.stdin.buffer.read()
    finally:
        os._exit(0)


def run_api(host: str, port: int) -> None:
    import uvicorn

    from backend.api.app import app
    from backend.db.database import get_db

    # Tauri starts the worker after /healthz becomes available. Complete migrations
    # before opening the API socket so both processes cannot migrate a fresh DB.
    get_db().init_schema()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


def run_worker() -> None:
    from backend.db.database import get_db
    from backend.db.repository import Repository
    from backend.workers.pipeline import PipelineWorker

    db = get_db()
    db.init_schema()
    worker = PipelineWorker(repository=Repository(db))
    logging.getLogger("nebula.launcher").info("Starting embedded Nebula pipeline worker")
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        logging.getLogger("nebula.launcher").info("Embedded Nebula pipeline worker stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Embedded Nebula Python services")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    api_parser = subparsers.add_parser("api", help="Run the FastAPI service")
    api_parser.add_argument("--host", default="127.0.0.1")
    api_parser.add_argument("--port", type=int, default=17843)
    subparsers.add_parser("worker", help="Run the background pipeline worker")

    args = parser.parse_args()
    if os.environ.get("NEBULA_PARENT_PIPE") == "1":
        threading.Thread(target=monitor_parent_pipe, name="parent-monitor", daemon=True).start()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.mode == "api":
        run_api(args.host, args.port)
    else:
        run_worker()


if __name__ == "__main__":
    main()
