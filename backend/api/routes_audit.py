from fastapi import APIRouter, Depends

from backend.security_auth import require_admin
from backend.storage.repositories import list_execution_audits, verify_audit_ledger

router = APIRouter(
    prefix="/audit",
    tags=["audit"],
    dependencies=[Depends(require_admin)],
)


@router.get("/executions")
def execution_audit_list(
    action: str | None = None,
    status: str | None = None,
    service_name: str | None = None,
    actor: str | None = None,
    before_sequence: int | None = None,
    limit: int = 100,
):
    effective_limit = max(1, min(limit, 500))
    audits = list_execution_audits(
        action=action,
        status=status,
        service_name=service_name,
        actor=actor,
        before_sequence=before_sequence,
        limit=effective_limit,
    )
    return {
        "audits": audits,
        "next_before_sequence": (
            audits[-1]["sequence"] if len(audits) == effective_limit else None
        ),
    }


@router.get("/verify")
def execution_audit_verify():
    return verify_audit_ledger()
