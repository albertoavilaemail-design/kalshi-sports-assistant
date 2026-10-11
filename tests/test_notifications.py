import logging

from helpers import FakeResponse, FakeSession, storage

from kalshi_assistant.config import NotifyConfig, SecretRedactingFilter
from kalshi_assistant.notifications import Notifier


def cfg():
    return NotifyConfig(enabled=True, dedup_minutes=60, telegram_bot_token="123:MOCKTOKEN", telegram_chat_id="42")


def test_send_and_deduplicate():
    sess = FakeSession([FakeResponse(200, {"ok": True})])
    n = Notifier(cfg(), storage(), session=sess)
    assert n.send("test", "k1", "hello")
    assert not n.send("test", "k1", "hello again")  # duplicate suppressed, no second HTTP call
    assert len(sess.calls) == 1
    method, url, body, _ = sess.calls[0]
    assert url.endswith("/sendMessage") and body["chat_id"] == "42" and "hello" in body["text"]


def test_recheck_failure_suppresses_actionable_alert():
    sess = FakeSession([])
    n = Notifier(cfg(), storage(), session=sess)
    assert not n.send("entry", "k2", "buy!", recheck=lambda: False)
    assert sess.calls == []
    assert n.history()[0]["error"] == "suppressed by price recheck"


def test_unconfigured_or_disabled_does_not_crash():
    st = storage()
    n = Notifier(NotifyConfig(enabled=True), st)
    assert not n.send("x", "k", "msg")
    assert not Notifier(NotifyConfig(enabled=False), st).send("x", "k3", "msg")


def test_telegram_http_error_is_recorded():
    n = Notifier(cfg(), storage(), session=FakeSession([FakeResponse(401, {})]))
    assert not n.send("x", "k4", "msg")
    assert "401" in n.history()[0]["error"]


def test_long_messages_are_chunked():
    sess = FakeSession([FakeResponse(200), FakeResponse(200)])
    assert Notifier(cfg(), storage(), session=sess).send("x", "k5", "a" * 5000)
    assert len(sess.calls) == 2


def test_secrets_are_redacted_from_logs():
    rec = logging.LogRecord("t", logging.INFO, "f", 1, "token=%s", ("123:MOCKTOKEN",), None)
    SecretRedactingFilter(["123:MOCKTOKEN"]).filter(rec)
    assert "MOCKTOKEN" not in rec.getMessage()
