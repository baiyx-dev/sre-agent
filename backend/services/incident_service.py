import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from backend.storage.db import get_conn


INCIDENT_STATUSES = {"open", "investigating", "mitigated", "resolved"}
_SEVERITY_RANK = {"info": 10, "warning": 20, "high": 30, "critical": 40}
_ALLOWED_TRANSITIONS = {
    "open": {"investigating", "mitigated", "resolved"},
    "investigating": {"mitigated", "resolved"},
    "mitigated": {"investigating", "resolved"},
    "resolved": {"open"},
}


class IncidentServiceError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _incident_row(row) -> dict | None:
    return dict(row) if row else None


def _normalized_title(title: str) -> str:
    value = re.sub(r"\s+", " ", title.strip().lower())
    value = re.sub(r"\b\d+(?:\.\d+)?%?\b", "#", value)
    return value[:300]


def _fingerprint(service_name: str | None, title: str) -> str:
    canonical = f"{(service_name or 'unknown').lower()}|{_normalized_title(title)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _alert_payload(alert: dict) -> dict:
    return {
        "id": alert.get("id"),
        "service": alert.get("service") or alert.get("service_name"),
        "severity": alert.get("severity") or "warning",
        "title": alert.get("title") or alert.get("message") or "Untitled alert",
        "message": str(alert.get("message") or "")[:2000],
        "created_at": alert.get("created_at") or alert.get("timestamp"),
        "resolved": bool(alert.get("resolved", False)),
    }


def correlate_alerts(alerts: list[dict], *, actor: str, source: str) -> list[dict]:
    touched_ids: list[str] = []
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        for raw_alert in alerts[:500]:
            if not isinstance(raw_alert, dict):
                continue
            payload = _alert_payload(raw_alert)
            service_name = payload["service"]
            title = str(payload["title"])[:500]
            severity = str(payload["severity"]).lower()
            if severity not in _SEVERITY_RANK:
                severity = "warning"
            observed_at = str(payload["created_at"] or _now())
            alert_ref = str(payload["id"] or hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest())
            fingerprint = _fingerprint(service_name, title)

            cur.execute("SELECT * FROM incidents WHERE fingerprint = ?", (fingerprint,))
            incident = cur.fetchone()
            created = False
            if not incident:
                incident_id = str(uuid.uuid4())
                now = _now()
                cur.execute(
                    """
                    INSERT INTO incidents (
                        id, fingerprint, title, service_name, severity, status,
                        alert_count, first_seen_at, last_seen_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?, ?, ?)
                    """,
                    (
                        incident_id,
                        fingerprint,
                        title,
                        service_name,
                        severity,
                        observed_at,
                        observed_at,
                        now,
                        now,
                    ),
                )
                created = True
            else:
                incident_id = incident["id"]

            cur.execute(
                """
                INSERT OR IGNORE INTO incident_alerts (
                    incident_id, alert_source, alert_ref, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    source,
                    alert_ref,
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
            attached = cur.rowcount == 1
            if not attached:
                if created:
                    cur.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
                continue

            cur.execute("SELECT severity, status FROM incidents WHERE id = ?", (incident_id,))
            current = cur.fetchone()
            escalated = severity if _SEVERITY_RANK[severity] > _SEVERITY_RANK.get(current["severity"], 0) else current["severity"]
            next_status = "open" if current["status"] == "resolved" else current["status"]
            cur.execute(
                """
                UPDATE incidents
                SET severity = ?, status = ?, alert_count = alert_count + 1,
                    last_seen_at = ?, updated_at = ?, resolved_at = NULL
                WHERE id = ?
                """,
                (escalated, next_status, observed_at, _now(), incident_id),
            )
            event_type = "incident_created" if created else "alert_correlated"
            cur.execute(
                """
                INSERT INTO incident_events (
                    incident_id, event_type, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    event_type,
                    actor,
                    json.dumps(
                        {"alert_source": source, "alert_ref": alert_ref, "severity": severity},
                        ensure_ascii=False,
                    ),
                    _now(),
                ),
            )
            if incident_id not in touched_ids:
                touched_ids.append(incident_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return [item for item in (get_incident(item_id) for item_id in touched_ids) if item]


def list_incidents(
    *,
    status: str | None = None,
    service_name: str | None = None,
    limit: int = 100,
) -> list[dict]:
    if status and status not in INCIDENT_STATUSES:
        raise IncidentServiceError(400, "invalid incident status")
    filters = []
    values: list[object] = []
    if status:
        filters.append("status = ?")
        values.append(status)
    if service_name:
        filters.append("service_name = ?")
        values.append(service_name)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    values.append(max(1, min(limit, 500)))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM incidents {where_clause} ORDER BY updated_at DESC LIMIT ?",
        tuple(values),
    )
    result = [_incident_row(row) for row in cur.fetchall()]
    conn.close()
    return result


def get_incident(incident_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    incident = _incident_row(cur.fetchone())
    if not incident:
        conn.close()
        return None
    cur.execute(
        """
        SELECT alert_source, alert_ref, payload_json, created_at
        FROM incident_alerts WHERE incident_id = ? ORDER BY id ASC LIMIT 200
        """,
        (incident_id,),
    )
    incident["alerts"] = [
        {
            "source": row["alert_source"],
            "ref": row["alert_ref"],
            "payload": _json_object(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in cur.fetchall()
    ]
    cur.execute(
        """
        SELECT id, event_type, actor, payload_json, created_at
        FROM incident_events WHERE incident_id = ? ORDER BY id ASC LIMIT 500
        """,
        (incident_id,),
    )
    incident["events"] = [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "payload": _json_object(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in cur.fetchall()
    ]
    conn.close()
    return incident


def update_incident(
    incident_id: str,
    *,
    actor: str,
    status: str | None = None,
    owner: str | None = None,
    summary: str | None = None,
) -> dict:
    current = get_incident(incident_id)
    if not current:
        raise IncidentServiceError(404, "incident not found")
    changes = {}
    if status is not None:
        if status not in INCIDENT_STATUSES:
            raise IncidentServiceError(400, "invalid incident status")
        if status != current["status"] and status not in _ALLOWED_TRANSITIONS[current["status"]]:
            raise IncidentServiceError(
                409,
                f"invalid incident transition: {current['status']} -> {status}",
            )
        changes["status"] = status
    if owner is not None:
        changes["owner"] = owner.strip()[:200] or None
    if summary is not None:
        changes["summary"] = summary.strip()[:4000] or None
    if not changes:
        return current

    next_status = changes.get("status", current["status"])
    now = _now()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        """
        UPDATE incidents
        SET status = ?, owner = ?, summary = ?, updated_at = ?, resolved_at = ?
        WHERE id = ?
        """,
        (
            next_status,
            changes.get("owner", current["owner"]),
            changes.get("summary", current["summary"]),
            now,
            now if next_status == "resolved" else None,
            incident_id,
        ),
    )
    cur.execute(
        """
        INSERT INTO incident_events (
            incident_id, event_type, actor, payload_json, created_at
        ) VALUES (?, 'incident_updated', ?, ?, ?)
        """,
        (incident_id, actor, json.dumps(changes, ensure_ascii=False), now),
    )
    conn.commit()
    conn.close()
    return get_incident(incident_id) or current


def get_incident_metrics() -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) AS count FROM incidents GROUP BY status")
    counts = {row["status"]: row["count"] for row in cur.fetchall()}
    conn.close()
    return {status: counts.get(status, 0) for status in sorted(INCIDENT_STATUSES)}
