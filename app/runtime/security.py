from __future__ import annotations

import re


SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|authorization|password|secret|cookie)[\"']?"
    r"\s*[:=]\s*)(?:Bearer\s+)?(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;}]+)"
)
URL_PASSWORD = re.compile(r"(://[^\s:/@]+:)([^\s@]+)(@)")


def safe_error_message(message: str, max_length: int = 4_000) -> str:
    redacted = SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", message)
    return URL_PASSWORD.sub(r"\1[REDACTED]\3", redacted)[:max_length]


def public_execution_error(error_type: str) -> str:
    normalized = error_type.upper()
    if "TIMEOUT" in normalized:
        return "Agent 调用超时"
    if "OUTPUT" in normalized or "REPAIR" in normalized or "SCHEMA" in normalized:
        return "Agent 输出未通过结构校验"
    if "TOOL" in normalized:
        return "Agent 工具调用失败"
    if "BRIDGE" in normalized or "PROTOCOL" in normalized:
        return "Agent 运行服务不可用"
    return "Agent 执行失败"
