from datetime import datetime

from backend.storage.db import get_conn
from backend.tools.service_tool import get_service_status


def rollback_service(service_name: str):
    current_service = get_service_status(service_name)
    if not current_service:
        return {
            "success": False,
            "message": f"service '{service_name}' not found"
        }

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, old_version, new_version, status, created_at
        FROM deployments
        WHERE service = ?
        ORDER BY id DESC
        LIMIT 1
    """, (service_name,))
    latest_deploy = cur.fetchone()

    if not latest_deploy:
        conn.close()
        return {
            "success": False,
            "message": f"no deployment history found for '{service_name}'"
        }

    rollback_version = latest_deploy["old_version"]
    current_version = current_service["version"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("""
        UPDATE services
        SET version = ?, status = ?, cpu = ?, memory = ?, error_rate = ?, last_deploy_time = ?
        WHERE name = ?
    """, (
        rollback_version,
        "running",
        28.0,
        45.0,
        0.2,
        now,
        service_name
    ))

    cur.execute("""
        INSERT INTO logs (service, timestamp, level, message)
        VALUES (?, ?, ?, ?)
    """, (service_name, now, "INFO", f"rolled back from {current_version} to {rollback_version}"))

    cur.execute("""
        UPDATE alerts
        SET resolved = 1
        WHERE service = ? AND resolved = 0
    """, (service_name,))

    cur.execute("""
        INSERT INTO deployments (service, old_version, new_version, status, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        service_name,
        current_version,
        rollback_version,
        "rollback_success",
        now
    ))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "service": service_name,
        "rolled_back_from": current_version,
        "rolled_back_to": rollback_version,
        "status": "running",
        "message": f"{service_name} rolled back to {rollback_version}"
    }