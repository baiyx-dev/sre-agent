from fastapi import APIRouter, Depends

from backend.security_auth import Principal, require_viewer


router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me")
def auth_me(principal: Principal = Depends(require_viewer)):
    return {
        "authenticated": True,
        "subject": principal.subject,
        "role": principal.role,
        "workspace_id": principal.workspace_id,
        "auth_source": principal.auth_source,
    }
