from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    confirm: bool = False
    pending_action: dict | None = None


class ConfirmActionRequest(BaseModel):
    pending_action: dict
    session_id: str | None = None
    dry_run: bool = False


class ChatResponse(BaseModel):
    intent: str
    steps: list
    final_answer: str
    assessment_details: dict | None = None
    requires_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[str] | None = None
    policy_decision: dict | None = None
    execution_mode: str | None = None
    requires_confirmation: bool = False
    pending_action: dict | None = None
    generation_source: str = "fallback_rule"
    llm_provider: str = "deepseek"
    used_fallback: bool = True
    fallback_reason: str | None = "rule_only"
    session_id: str | None = None
