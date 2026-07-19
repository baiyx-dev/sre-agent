import os
from contextvars import ContextVar, Token
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


_USAGE_CAPTURE: ContextVar[dict | None] = ContextVar("sre_llm_usage_capture", default=None)


def begin_llm_usage_capture() -> Token:
    return _USAGE_CAPTURE.set(
        {
            "call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd_micros": 0,
            "providers": set(),
            "models": set(),
        }
    )


def finish_llm_usage_capture(token: Token) -> dict:
    captured = _USAGE_CAPTURE.get() or {}
    result = {
        "call_count": int(captured.get("call_count", 0)),
        "input_tokens": int(captured.get("input_tokens", 0)),
        "output_tokens": int(captured.get("output_tokens", 0)),
        "total_tokens": int(captured.get("total_tokens", 0)),
        "cost_usd_micros": int(captured.get("cost_usd_micros", 0)),
        "providers": sorted(captured.get("providers", set())),
        "models": sorted(captured.get("models", set())),
    }
    _USAGE_CAPTURE.reset(token)
    return result


def _non_negative_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _price_per_million(env_name: str) -> Decimal:
    try:
        value = Decimal(os.getenv(env_name, "0").strip() or "0")
    except InvalidOperation:
        return Decimal("0")
    return max(Decimal("0"), value)


def _cost_micros(input_tokens: int, output_tokens: int) -> int:
    # USD/million-token multiplied by tokens is numerically equal to USD micros.
    value = (
        Decimal(input_tokens) * _price_per_million("SRE_LLM_INPUT_COST_PER_MILLION_USD")
        + Decimal(output_tokens) * _price_per_million("SRE_LLM_OUTPUT_COST_PER_MILLION_USD")
    )
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def record_llm_usage(provider: str, model: str, usage: dict | None) -> None:
    captured = _USAGE_CAPTURE.get()
    if captured is None:
        return
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = _non_negative_int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0))
    )
    output_tokens = _non_negative_int(
        usage.get("completion_tokens", usage.get("output_tokens", 0))
    )
    total_tokens = _non_negative_int(usage.get("total_tokens"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens

    captured["call_count"] += 1
    captured["input_tokens"] += input_tokens
    captured["output_tokens"] += output_tokens
    captured["total_tokens"] += total_tokens
    captured["cost_usd_micros"] += _cost_micros(input_tokens, output_tokens)
    if provider:
        captured["providers"].add(str(provider)[:80])
    if model:
        captured["models"].add(str(model)[:120])
