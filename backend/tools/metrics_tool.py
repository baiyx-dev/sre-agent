from backend.tools.service_tool import get_service_status
from backend.tools.external_data_source import get_external_metrics

#将数据库中的完整服务记录，过滤/映射成只包含监控指标的字典。
def get_service_metrics(service_name: str):
    external = get_external_metrics(service_name)
    if external is not None:
        return external

    service = get_service_status(service_name)
    if not service:
        return None

    return {
        "service": service["name"],
        "cpu": service["cpu"],
        "memory": service["memory"],
        "error_rate": service["error_rate"],
        "replicas": service["replicas"],
        "status": service["status"],
    }
