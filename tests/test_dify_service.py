"""
DifyChatService 的單元測試：透過 SDK session.app.chat.invoke 呼叫 Chatflow，
並在超過逾時秒數時拋出 DifyTimeoutError，而不是無限等待。
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.dify_service import DifyChatService, DifyTimeoutError


def _session(invoke):
    return SimpleNamespace(app=SimpleNamespace(chat=SimpleNamespace(invoke=invoke)))


def test_chat_returns_answer_and_conversation_id():
    invoke = MagicMock(return_value={"answer": "hi", "conversation_id": "c1"})
    svc = DifyChatService(_session(invoke), app_id="app-1", timeout=5)
    answer, cid = svc.chat("hello", {"user_id": "U"}, conversation_id=None)
    assert (answer, cid) == ("hi", "c1")
    kwargs = invoke.call_args.kwargs
    assert kwargs["app_id"] == "app-1"
    assert kwargs["response_mode"] == "blocking"
    assert "conversation_id" not in kwargs


def test_chat_passes_existing_conversation_id():
    invoke = MagicMock(return_value={"answer": "x", "conversation_id": "c1"})
    svc = DifyChatService(_session(invoke), app_id="app-1", timeout=5)
    svc.chat("hello", {}, conversation_id="c1")
    assert invoke.call_args.kwargs["conversation_id"] == "c1"


def test_chat_raises_timeout_error_when_dify_is_too_slow():
    def slow(**kwargs):
        time.sleep(0.5)
        return {"answer": "late"}
    svc = DifyChatService(_session(slow), app_id="app-1", timeout=0.1)
    with pytest.raises(DifyTimeoutError):
        svc.chat("hello", {}, conversation_id=None)
