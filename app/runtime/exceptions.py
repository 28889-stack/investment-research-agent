class RuntimeErrorBase(Exception):
    code = "RUNTIME_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class ProfileNotFoundError(RuntimeErrorBase):
    code = "PROFILE_NOT_FOUND"


class ProfileValidationError(RuntimeErrorBase):
    code = "PROFILE_INVALID"


class ToolNotAllowedError(RuntimeErrorBase):
    code = "TOOL_NOT_ALLOWED"


class ToolBudgetExhaustedError(RuntimeErrorBase):
    code = "TOOL_BUDGET_EXHAUSTED"


class ToolInputValidationError(RuntimeErrorBase):
    code = "TOOL_INPUT_INVALID"


class ToolTimeoutError(RuntimeErrorBase):
    code = "TOOL_TIMEOUT"


class ContextTooLargeError(RuntimeErrorBase):
    code = "CONTEXT_TOO_LARGE"


class BridgeStartError(RuntimeErrorBase):
    code = "BRIDGE_START_ERROR"


class BridgeProtocolError(RuntimeErrorBase):
    code = "BRIDGE_PROTOCOL_ERROR"


class BridgeCrashedError(RuntimeErrorBase):
    code = "BRIDGE_CRASHED"


class AgentTimeoutError(RuntimeErrorBase):
    code = "AGENT_TIMEOUT"


class AgentOutputError(RuntimeErrorBase):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class OutputSchemaError(AgentOutputError):
    pass


class CheckpointError(RuntimeErrorBase):
    code = "CHECKPOINT_ERROR"
