import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from backend.storage.db import (
    AUDIT_LEDGER_LOCK_ID,
    configured_workspace_id,
    get_conn,
    is_postgres_database,
)
from backend.storage.repositories import verify_audit_ledger


_RETENTION_DEFAULTS = {
    "logs": ("SRE_RETENTION_LOG_DAYS", 30),
    "chat_sessions": ("SRE_RETENTION_CHAT_DAYS", 90),
    "task_runs": ("SRE_RETENTION_TASK_DAYS", 180),
    "usage_events": ("SRE_RETENTION_USAGE_DAYS", 400),
    "incidents": ("SRE_RETENTION_INCIDENT_DAYS", 730),
    "change_requests": ("SRE_RETENTION_CHANGE_DAYS", 730),
    "workspace_api_keys": ("SRE_RETENTION_REVOKED_KEY_DAYS", 730),
    "trial_activation_attempts": ("SRE_RETENTION_TRIAL_ATTEMPT_DAYS", 30),
    "trial_feedback": ("SRE_RETENTION_TRIAL_FEEDBACK_DAYS", 730),
    "execution_audits": ("SRE_RETENTION_AUDIT_DAYS", 2555),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sqlite_database(path: Path) -> dict:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"database backup not found: {resolved}")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
    finally:
        conn.close()
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {integrity}")
    return {
        "path": str(resolved),
        "integrity": integrity,
        "table_count": table_count,
        "size_bytes": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def backup_database(output_dir: Path) -> dict:
    if is_postgres_database():
        raise RuntimeError("PostgreSQL backups must use pg_dump or the managed database backup service")
    resolved_dir = output_dir.resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = resolved_dir / f"sre-agent-{timestamp}.sqlite3"
    if backup_path.exists():
        raise FileExistsError(f"backup already exists: {backup_path}")

    source = get_conn()
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    verification = verify_sqlite_database(backup_path)
    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **verification,
    }
    manifest_path = backup_path.with_suffix(backup_path.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {**manifest, "manifest_path": str(manifest_path)}


def restore_database(source_path: Path, target_path: Path, *, force: bool = False) -> dict:
    source = source_path.resolve()
    target = target_path.resolve()
    verify_sqlite_database(source)
    if source == target:
        raise ValueError("restore source and target must be different files")
    if target.exists() and not force:
        raise FileExistsError("restore target already exists; pass --force to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)

    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    return verify_sqlite_database(target)


def _retention_days(env_name: str, default: int) -> int:
    try:
        value = int(os.getenv(env_name, str(default)).strip())
    except ValueError as exc:
        raise ValueError(f"{env_name} must be an integer") from exc
    if value < 1 or value > 36_500:
        raise ValueError(f"{env_name} must be between 1 and 36500 days")
    return value


def _retention_cutoffs(now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    result = {}
    for name, (env_name, default) in _RETENTION_DEFAULTS.items():
        cutoff = current - timedelta(days=_retention_days(env_name, default))
        result[name] = {
            "days": _retention_days(env_name, default),
            "iso": cutoff.isoformat(),
            "legacy": cutoff.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return result


def _audit_prunable_prefix(cur, cutoff: str) -> dict:
    """Return only the expired contiguous prefix so the remaining chain is valid."""
    cur.execute(
        """
        SELECT candidate.sequence, candidate.entry_hash
        FROM audit_ledger AS candidate
        WHERE candidate.created_at < ?
          AND NOT EXISTS (
              SELECT 1 FROM audit_ledger AS earlier
              WHERE earlier.sequence < candidate.sequence
                AND earlier.created_at >= ?
          )
        ORDER BY candidate.sequence DESC
        LIMIT 1
        """,
        (cutoff, cutoff),
    )
    last = cur.fetchone()
    if not last:
        return {"count": 0, "through_sequence": None, "head_hash": None}
    cur.execute(
        "SELECT COUNT(*) AS count FROM audit_ledger WHERE sequence <= ?",
        (last["sequence"],),
    )
    count_row = cur.fetchone()
    return {
        "count": int(count_row["count"] if count_row else 0),
        "through_sequence": int(last["sequence"]),
        "head_hash": last["entry_hash"],
    }


def _billing_statement_guard(cur, usage_cutoff: str) -> dict:
    environment = os.getenv("SRE_ENVIRONMENT", "development").strip().lower()
    enabled = os.getenv(
        "SRE_REQUIRE_FINALIZED_BILLING_BEFORE_USAGE_PURGE",
        "true" if environment == "production" else "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    cur.execute(
        """
        SELECT DISTINCT substr(usage.occurred_at, 1, 7) AS month
        FROM usage_events AS usage
        WHERE usage.workspace_id = ? AND usage.occurred_at < ?
          AND NOT EXISTS (
              SELECT 1 FROM billing_statements AS bill
              WHERE bill.workspace_id = usage.workspace_id
                AND bill.month = substr(usage.occurred_at, 1, 7)
          )
        ORDER BY month ASC
        """,
        (configured_workspace_id(), usage_cutoff),
    )
    months = [row["month"] for row in cur.fetchall() if row["month"]]
    return {
        "enabled": enabled,
        "unfinalized_usage_months": months,
        "blocked": enabled and bool(months),
    }


def retention_preview(now: datetime | None = None) -> dict:
    cutoffs = _retention_cutoffs(now)
    conn = get_conn()
    cur = conn.cursor()

    def count(sql: str, params: tuple) -> int:
        cur.execute(sql, params)
        row = cur.fetchone()
        return int(row["count"] if row else 0)

    terminal_statuses = "'executed','dry_run','denied','failed','unknown','cancelled','expired'"
    audit_prefix = _audit_prunable_prefix(cur, cutoffs["execution_audits"]["iso"])
    billing_guard = _billing_statement_guard(cur, cutoffs["usage_events"]["iso"])
    candidates = {
        "logs": count("SELECT COUNT(*) AS count FROM logs WHERE timestamp < ?", (cutoffs["logs"]["legacy"],)),
        "chat_sessions": count(
            "SELECT COUNT(*) AS count FROM chat_sessions WHERE updated_at < ?",
            (cutoffs["chat_sessions"]["legacy"],),
        ),
        "task_runs": count(
            "SELECT COUNT(*) AS count FROM task_runs WHERE created_at < ?",
            (cutoffs["task_runs"]["legacy"],),
        ),
        "task_steps": count(
            """
            SELECT COUNT(*) AS count FROM task_steps
            WHERE task_run_id IN (SELECT id FROM task_runs WHERE created_at < ?)
            """,
            (cutoffs["task_runs"]["legacy"],),
        ),
        "usage_events": count(
            "SELECT COUNT(*) AS count FROM usage_events WHERE occurred_at < ?",
            (cutoffs["usage_events"]["iso"],),
        ),
        "resolved_incidents": count(
            """
            SELECT COUNT(*) AS count FROM incidents
            WHERE status = 'resolved' AND resolved_at IS NOT NULL AND resolved_at < ?
            """,
            (cutoffs["incidents"]["iso"],),
        ),
        "terminal_change_requests": count(
            f"""
            SELECT COUNT(*) AS count FROM change_requests
            WHERE status IN ({terminal_statuses})
              AND completed_at IS NOT NULL AND completed_at < ?
            """,
            (cutoffs["change_requests"]["iso"],),
        ),
        "revoked_api_keys": count(
            """
            SELECT COUNT(*) AS count FROM workspace_api_keys
            WHERE revoked_at IS NOT NULL AND revoked_at < ?
            """,
            (cutoffs["workspace_api_keys"]["iso"],),
        ),
        "trial_activation_attempts": count(
            "SELECT COUNT(*) AS count FROM trial_activation_attempts WHERE attempted_at < ?",
            (cutoffs["trial_activation_attempts"]["iso"],),
        ),
        "trial_feedback": count(
            "SELECT COUNT(*) AS count FROM trial_feedback WHERE created_at < ?",
            (cutoffs["trial_feedback"]["iso"],),
        ),
        "execution_audit_projections": count(
            "SELECT COUNT(*) AS count FROM execution_audits WHERE created_at < ?",
            (cutoffs["execution_audits"]["iso"],),
        ),
        "audit_ledger_entries": audit_prefix["count"],
    }
    conn.close()
    return {
        "mode": "dry_run",
        "workspace_id": configured_workspace_id(),
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "policies": {
            name: {"days": value["days"], "cutoff": value["iso"]}
            for name, value in cutoffs.items()
        },
        "candidates": candidates,
        "candidate_total": sum(candidates.values()),
        "billing_statement_guard": billing_guard,
    }


def purge_retained_data(
    *,
    confirmation: str,
    now: datetime | None = None,
) -> dict:
    expected = f"PURGE:{configured_workspace_id()}"
    if confirmation != expected:
        raise PermissionError(f"confirmation must exactly equal {expected}")
    audit_integrity = verify_audit_ledger()
    if not audit_integrity["valid"]:
        raise RuntimeError("audit ledger verification failed; refusing retention purge")
    preview = retention_preview(now)
    if preview["billing_statement_guard"]["blocked"]:
        months = ", ".join(
            preview["billing_statement_guard"]["unfinalized_usage_months"]
        )
        raise RuntimeError(
            "usage retention is blocked until billing statements are finalized for: "
            f"{months}"
        )
    cutoffs = _retention_cutoffs(now)
    conn = get_conn()
    cur = conn.cursor()
    terminal_statuses = "'executed','dry_run','denied','failed','unknown','cancelled','expired'"
    deleted = {}

    def execute(name: str, sql: str, params: tuple) -> None:
        cur.execute(sql, params)
        deleted[name] = max(0, int(cur.rowcount))

    try:
        cur.execute("BEGIN IMMEDIATE")
        if is_postgres_database():
            cur.execute("SELECT pg_advisory_xact_lock(?)", (AUDIT_LEDGER_LOCK_ID,))
        execute(
            "task_steps",
            "DELETE FROM task_steps WHERE task_run_id IN (SELECT id FROM task_runs WHERE created_at < ?)",
            (cutoffs["task_runs"]["legacy"],),
        )
        execute("task_runs", "DELETE FROM task_runs WHERE created_at < ?", (cutoffs["task_runs"]["legacy"],))
        execute(
            "incident_alerts",
            """
            DELETE FROM incident_alerts WHERE incident_id IN (
                SELECT id FROM incidents
                WHERE status = 'resolved' AND resolved_at IS NOT NULL AND resolved_at < ?
            )
            """,
            (cutoffs["incidents"]["iso"],),
        )
        execute(
            "incident_events",
            """
            DELETE FROM incident_events WHERE incident_id IN (
                SELECT id FROM incidents
                WHERE status = 'resolved' AND resolved_at IS NOT NULL AND resolved_at < ?
            )
            """,
            (cutoffs["incidents"]["iso"],),
        )
        execute(
            "incidents",
            "DELETE FROM incidents WHERE status = 'resolved' AND resolved_at IS NOT NULL AND resolved_at < ?",
            (cutoffs["incidents"]["iso"],),
        )
        execute(
            "change_jobs",
            f"""
            DELETE FROM change_jobs WHERE change_request_id IN (
                SELECT id FROM change_requests WHERE status IN ({terminal_statuses})
                  AND completed_at IS NOT NULL AND completed_at < ?
            )
            """,
            (cutoffs["change_requests"]["iso"],),
        )
        execute(
            "change_requests",
            f"""
            DELETE FROM change_requests WHERE status IN ({terminal_statuses})
              AND completed_at IS NOT NULL AND completed_at < ?
            """,
            (cutoffs["change_requests"]["iso"],),
        )
        execute("logs", "DELETE FROM logs WHERE timestamp < ?", (cutoffs["logs"]["legacy"],))
        execute(
            "chat_sessions",
            "DELETE FROM chat_sessions WHERE updated_at < ?",
            (cutoffs["chat_sessions"]["legacy"],),
        )
        execute(
            "usage_events",
            "DELETE FROM usage_events WHERE occurred_at < ?",
            (cutoffs["usage_events"]["iso"],),
        )
        execute(
            "workspace_api_keys",
            "DELETE FROM workspace_api_keys WHERE revoked_at IS NOT NULL AND revoked_at < ?",
            (cutoffs["workspace_api_keys"]["iso"],),
        )
        execute(
            "trial_activation_attempts",
            "DELETE FROM trial_activation_attempts WHERE attempted_at < ?",
            (cutoffs["trial_activation_attempts"]["iso"],),
        )
        execute(
            "trial_feedback",
            "DELETE FROM trial_feedback WHERE created_at < ?",
            (cutoffs["trial_feedback"]["iso"],),
        )
        audit_prefix = _audit_prunable_prefix(cur, cutoffs["execution_audits"]["iso"])
        if audit_prefix["count"]:
            cur.execute(
                """
                SELECT pruned_entry_count
                FROM audit_ledger_checkpoints
                ORDER BY id DESC
                LIMIT 1
                """
            )
            prior_checkpoint = cur.fetchone()
            prior_count = int(prior_checkpoint["pruned_entry_count"]) if prior_checkpoint else 0
            cur.execute(
                """
                INSERT INTO audit_ledger_checkpoints (
                    pruned_through_sequence, pruned_entry_count, head_hash,
                    created_at, reason
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    audit_prefix["through_sequence"],
                    prior_count + audit_prefix["count"],
                    audit_prefix["head_hash"],
                    (now or datetime.now(timezone.utc)).isoformat(),
                    "retention_policy",
                ),
            )
        execute(
            "audit_ledger_entries",
            "DELETE FROM audit_ledger WHERE sequence <= ?",
            (audit_prefix["through_sequence"] or 0,),
        )
        execute(
            "execution_audit_projections",
            "DELETE FROM execution_audits WHERE created_at < ?",
            (cutoffs["execution_audits"]["iso"],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        **preview,
        "mode": "applied",
        "confirmation": expected,
        "deleted": deleted,
        "deleted_total": sum(deleted.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="SRE Agent database maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Create and verify an online backup")
    backup_parser.add_argument("--output-dir", required=True, type=Path)

    verify_parser = subparsers.add_parser("verify", help="Verify a SQLite backup")
    verify_parser.add_argument("--source", required=True, type=Path)

    restore_parser = subparsers.add_parser("restore", help="Restore into an explicit target file")
    restore_parser.add_argument("--source", required=True, type=Path)
    restore_parser.add_argument("--target", required=True, type=Path)
    restore_parser.add_argument("--force", action="store_true")

    purge_parser = subparsers.add_parser("purge", help="Preview or apply retention policies")
    purge_parser.add_argument("--apply", action="store_true")
    purge_parser.add_argument("--confirm", default="")

    args = parser.parse_args()
    if args.command == "backup":
        result = backup_database(args.output_dir)
    elif args.command == "verify":
        result = verify_sqlite_database(args.source)
    elif args.command == "restore":
        result = restore_database(args.source, args.target, force=args.force)
    elif args.apply:
        result = purge_retained_data(confirmation=args.confirm)
    else:
        result = retention_preview()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
