import json
import logging

from app.logging_config import configure_logging, redact_sensitive
from app.runtime.security import safe_error_message


def test_redaction_covers_sensitive_assignments_and_headers() -> None:
    raw = (
        "api_key=alpha token: beta password=gamma secret=delta "
        "cookie=session Authorization: Bearer auth-value "
        "sqlite://user:db-password@localhost/research"
    )

    safe = redact_sensitive(raw)

    for secret in ("alpha", "beta", "gamma", "delta", "session", "auth-value", "db-password"):
        assert secret not in safe
    assert safe.count("[REDACTED]") >= 7


def test_redaction_covers_quoted_json_and_multiword_secrets() -> None:
    raw = '{"api_key":"two word secret", "token": "quoted-token"}'

    assert "two word secret" not in redact_sensitive(raw)
    assert "quoted-token" not in safe_error_message(raw)


def test_structured_rotating_log_contains_required_fields_without_secrets(settings) -> None:
    logger = logging.getLogger("test.phase6.web")
    logger.handlers.clear()
    logger.propagate = False
    configure_logging(settings, "web", logger=logger)

    logger.warning(
        "provider failed api_key=do-not-log",
        extra={"run_id": "run-1", "workflow": "technical_v1", "node": "kronos", "status": "FAILED"},
    )
    for handler in logger.handlers:
        handler.flush()

    payload = json.loads((settings.logs_dir / "web.log").read_text(encoding="utf-8").splitlines()[-1])
    assert payload["component"] == "web"
    assert payload["run_id"] == "run-1"
    assert payload["workflow"] == "technical_v1"
    assert payload["node"] == "kronos"
    assert payload["status"] == "FAILED"
    assert payload["execution_id"] is None
    assert payload["duration_ms"] is None
    assert payload["error_type"] is None
    assert "do-not-log" not in json.dumps(payload)
    assert logger.handlers[-1].maxBytes == 10 * 1024 * 1024
    assert logger.handlers[-1].backupCount == 5


def test_persisted_error_redaction_covers_authorization_cookie_and_database_password() -> None:
    safe = safe_error_message(
        "Authorization: Bearer auth-secret cookie=session-secret "
        "sqlite://user:db-secret@localhost/research"
    )

    assert "auth-secret" not in safe
    assert "session-secret" not in safe
    assert "db-secret" not in safe
