from backend.storage.db import get_conn
from backend.tools.external_data_source import get_external_logs


def get_recent_logs(service_name: str, limit: int = 10):
    external = get_external_logs(service_name=service_name, limit=limit)
    if external is not None:
        return external

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT service, timestamp, level, message
        FROM logs
        WHERE service = ?
        ORDER BY id DESC
        LIMIT ?
    """, (service_name, limit))
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]
