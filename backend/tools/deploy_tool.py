from datetime import datetime

from backend.storage.db import get_conn
from backend.tools.service_tool import get_service_status


def deploy_service(service_name: str, new_version: str):
    service = get_service_status(service_name)
    if not service:
        return {
            "success": False,
            "message": f"service '{service_name}' not found"
        }

    conn = get_conn()
    cur = conn.cursor()

    old_version = service["version"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 模拟部署结果：
    # payment-service 部署到 v1.2.3 时故意制造一次失败/降级场景，便于后面排障演示
    if service_name == "payment-service" and new_version == "v1.2.3":
        new_status = "degraded"
        new_cpu = 75.0
        new_memory = 78.0
        new_error_rate = 15.6
        deploy_status = "success_with_risk"

        cur.execute("""
            INSERT INTO alerts (service, severity, title, message, created_at, resolved)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            service_name,
            "critical",
            f"{service_name} error rate high after deploy",
            f"{service_name} error rate increased after deployment to {new_version}",
            now,
            0
        ))

        cur.execute("""
            INSERT INTO logs (service, timestamp, level, message)
            VALUES (?, ?, ?, ?)
        """, (service_name, now, "ERROR", "database connection timeout after deployment"))

    else:
        new_status = "running"
        new_cpu = 30.0
        new_memory = 50.0
        new_error_rate = 0.3
        deploy_status = "success"

        cur.execute("""
            INSERT INTO logs (service, timestamp, level, message)
            VALUES (?, ?, ?, ?)
        """, (service_name, now, "INFO", f"deployment to {new_version} completed successfully"))

    cur.execute("""
        UPDATE services
        SET version = ?, status = ?, cpu = ?, memory = ?, error_rate = ?, last_deploy_time = ?
        WHERE name = ?
    """, (
        new_version,
        new_status,
        new_cpu,
        new_memory,
        new_error_rate,
        now,
        service_name
    ))

    cur.execute("""
        INSERT INTO deployments (service, old_version, new_version, status, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        service_name,
        old_version,
        new_version,
        deploy_status,
        now
    ))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "service": service_name,
        "old_version": old_version,
        "new_version": new_version,
        "status": new_status,
        "error_rate": new_error_rate,
        "deploy_status": deploy_status,
        "message": f"{service_name} deployed to {new_version}"
    }