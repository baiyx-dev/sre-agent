from backend.storage.db import get_conn
from backend.tools.external_data_source import get_external_service_status, get_external_services


def _list_local_services():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM services ORDER BY name")
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _merge_services(local_services: list[dict], external_services: list[dict] | None):
    if not external_services:
        return local_services

    merged = {}
    for svc in local_services:
        name = svc.get("name")
        if name:
            merged[name] = svc

    for svc in external_services:
        if isinstance(svc, dict) and svc.get("name"):
            merged[svc["name"]] = svc

    return [merged[name] for name in sorted(merged.keys())]


#列出所有服务
def list_services():
    external = get_external_services()
    local = _list_local_services()
    return _merge_services(local, external)

#查询单个服务
def get_service_status(service_name: str):
    external = get_external_service_status(service_name)
    if external is not None:
        return external

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM services WHERE name = ?", (service_name,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None
