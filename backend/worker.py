import argparse
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone

from backend.logging_config import configure_logging
from backend.services.change_worker_service import process_next_change_job
from backend.storage.db import init_db
from backend.storage.repositories import touch_worker_heartbeat, worker_heartbeat_status


def worker_healthcheck() -> bool:
    """Return whether this deployment has a fresh, active worker heartbeat."""
    try:
        return bool(worker_heartbeat_status()["healthy"])
    except Exception:
        logging.getLogger("sre-agent.worker").exception("worker_healthcheck_failed")
        return False


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="SRE Agent durable change worker")
    parser.add_argument("--once", action="store_true", help="Process at most one available job")
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Exit successfully when a fresh worker heartbeat is present",
    )
    args = parser.parse_args()

    if args.healthcheck:
        raise SystemExit(0 if worker_healthcheck() else 1)

    init_db()
    worker_id = os.getenv("SRE_CHANGE_WORKER_ID", "").strip() or (
        f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    )
    hostname = socket.gethostname()
    process_id = os.getpid()
    started_at = datetime.now(timezone.utc).isoformat()
    raw_interval = os.getenv("SRE_CHANGE_WORKER_POLL_SECONDS", "1").strip()
    try:
        poll_seconds = max(0.2, min(float(raw_interval), 30.0))
    except ValueError as exc:
        raise SystemExit("SRE_CHANGE_WORKER_POLL_SECONDS must be numeric") from exc
    raw_heartbeat_interval = os.getenv(
        "SRE_WORKER_HEARTBEAT_INTERVAL_SECONDS", "10"
    ).strip()
    try:
        heartbeat_interval = max(1.0, min(float(raw_heartbeat_interval), 30.0))
    except ValueError as exc:
        raise SystemExit("SRE_WORKER_HEARTBEAT_INTERVAL_SECONDS must be numeric") from exc

    touch_worker_heartbeat(
        worker_id,
        hostname=hostname,
        process_id=process_id,
        status="starting",
        started_at=started_at,
    )
    next_heartbeat_at = time.monotonic()
    try:
        while True:
            if time.monotonic() >= next_heartbeat_at:
                touch_worker_heartbeat(
                    worker_id,
                    hostname=hostname,
                    process_id=process_id,
                    status="polling",
                    started_at=started_at,
                )
                next_heartbeat_at = time.monotonic() + heartbeat_interval
            processed = process_next_change_job(worker_id)
            if processed:
                touch_worker_heartbeat(
                    worker_id,
                    hostname=hostname,
                    process_id=process_id,
                    status="idle",
                    started_at=started_at,
                )
                next_heartbeat_at = time.monotonic() + heartbeat_interval
            if args.once:
                return
            if not processed:
                time.sleep(poll_seconds)
    finally:
        try:
            touch_worker_heartbeat(
                worker_id,
                hostname=hostname,
                process_id=process_id,
                status="stopped",
                started_at=started_at,
            )
        except Exception:
            logging.getLogger("sre-agent.worker").exception(
                "worker_heartbeat_stop_failed worker_id=%s", worker_id
            )


if __name__ == "__main__":
    main()
