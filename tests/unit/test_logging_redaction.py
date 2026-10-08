"""Secret redaction in logs (required test #8)."""

from __future__ import annotations

import io
import logging

from factory.observability.logging import (
    SecretRedactionFilter,
    bind_context,
    clear_context,
    configure_logging,
)
from factory.security.redaction import register_secret, unregister_all_secrets

SECRET = "cc_log_test_secret_value_987"


def _make_record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def setup_function():
    unregister_all_secrets()
    register_secret(SECRET)


def teardown_function():
    unregister_all_secrets()
    clear_context()


def test_filter_redacts_registered_secret_in_message():
    filt = SecretRedactionFilter()
    record = _make_record(f"auth failed for key {SECRET}")
    assert filt.filter(record)
    assert SECRET not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()


def test_filter_redacts_extra_fields():
    filt = SecretRedactionFilter()
    record = _make_record("event", api_key=SECRET, nested={"token": SECRET})
    assert filt.filter(record)
    assert vars(record)["api_key"] == "[REDACTED]"
    assert vars(record)["nested"] == {"token": "[REDACTED]"}


def test_filter_redacts_args():
    filt = SecretRedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="key %s leaked",
        args=(SECRET,),
        exc_info=None,
    )
    assert filt.filter(record)
    assert SECRET not in record.getMessage()


def test_json_formatter_output_contains_no_secret():
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    logger = logging.getLogger("test.redaction")
    token = bind_context(job_id="job-1", api_key=SECRET)
    try:
        logger.info("request failed with key %s", SECRET)
    finally:
        clear_context(token)
    output = stream.getvalue()
    assert SECRET not in output
    assert "[REDACTED]" in output
    assert "job-1" in output  # non-secret context survives


def test_cc_pattern_redacted_in_logs_without_registration():
    unregister_all_secrets()
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    logger = logging.getLogger("test.redaction2")
    logger.info("got key cc_unregistered_pattern_123456")
    assert "cc_unregistered_pattern_123456" not in stream.getvalue()
