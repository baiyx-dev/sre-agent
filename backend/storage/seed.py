from datetime import datetime, timedelta

from backend.storage.db import get_conn


def _insert_seed_rows(cur, now: datetime):
    services = [
        (
            "payment-service",
            "v1.2.2",
            "degraded",
            78.0,
            69.0,
            12.5,
            3,
            (now - timedelta(minutes=18)).strftime("%Y-%m-%d %H:%M:%S"),
        ),
        (
            "order-service",
            "v2.4.1",
            "running",
            31.0,
            45.0,
            0.2,
            2,
            (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ]
    cur.executemany(
        """
        INSERT INTO services(name, version, status, cpu, memory, error_rate, replicas, last_deploy_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        services,
    )

    cur.execute(
        """
        INSERT INTO alerts(service, severity, title, message, created_at, resolved)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "payment-service",
            "critical",
            "error rate spike",
            "payment-service error rate exceeded threshold in the last 10 minutes",
            (now - timedelta(minutes=8)).strftime("%Y-%m-%d %H:%M:%S"),
            0,
        ),
    )

    logs = [
        (
            "payment-service",
            (now - timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S"),
            "ERROR",
            "database connection timeout while processing charge request",
        ),
        (
            "payment-service",
            (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            "WARN",
            "retrying upstream request after database connection timeout",
        ),
        (
            "payment-service",
            (now - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S"),
            "ERROR",
            "payment callback failed due to dependency timeout",
        ),
    ]
    cur.executemany(
        """
        INSERT INTO logs(service, timestamp, level, message)
        VALUES (?, ?, ?, ?)
        """,
        logs,
    )

    cur.execute(
        """
        INSERT INTO deployments(service, old_version, new_version, status, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "payment-service",
            "v1.2.1",
            "v1.2.2",
            "success",
            (now - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )


def seed_data():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS count FROM services")
    service_count = cur.fetchone()["count"]
    if service_count > 0:
        conn.close()
        return None

    now = datetime.now()
    _insert_seed_rows(cur, now)

    conn.commit()
    conn.close()
    return None


def reset_seed_data():
    conn = get_conn()
    cur = conn.cursor()
    for table in ("services", "alerts", "logs", "deployments", "task_steps", "task_runs", "execution_audits"):
        cur.execute(f"DELETE FROM {table}")
    now = datetime.now()
    _insert_seed_rows(cur, now)
    conn.commit()
    conn.close()
    return None
