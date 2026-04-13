from backend.storage.db import get_conn
from backend.tools.external_data_source import get_external_alerts


def get_recent_alerts(service_name: str | None = None, unresolved_only: bool = True, limit: int = 10):
    external = get_external_alerts(service_name=service_name, unresolved_only=unresolved_only, limit=limit)
    if external is not None:
        return external

    conn = get_conn()
    cur = conn.cursor()

    sql = """
        SELECT id, service, severity, title, message, created_at, resolved
        FROM alerts
    """
    conditions = []
    params = []

    if service_name:
        conditions.append("service = ?")
        params.append(service_name)

    if unresolved_only:
        conditions.append("resolved = 0")

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    return [dict(row) for row in rows]
