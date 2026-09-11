"""
LineEndpoint 的整合測試：以假的 LineClient 與 SDK session 走完整 webhook 流程。
驗證：簽名錯誤回 400、Verify 空事件回 200、文字訊息呼叫 Dify 並回覆、
去重、/clearconversationhistory、follow 歡迎詞、debug_mode 錯誤回傳、顯示名稱 inputs。
"""
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from core.dify_service import DifyTimeoutError
from endpoints.linebot import LineEndpoint
from tests.test_storage import FakeStorage

SECRET = "sec"
TOKEN = "tok"


def _sign(body: str) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()).decode()


def _request(body: dict, signature: str = None) -> Request:
    raw = json.dumps(body)
    headers = {"X-Line-Signature": signature if signature is not None else _sign(raw)}
    return Request(EnvironBuilder(method="POST", data=raw, headers=headers,
                                  content_type="application/json").get_environ())


def _text_event(text="hi", user="U1", event_id="evt-1", **extra_source):
    return {
        "type": "message", "replyToken": "rt-1", "webhookEventId": event_id,
        "deliveryContext": {"isRedelivery": False},
        "source": {"type": "user", "userId": user, **extra_source},
        "message": {"type": "text", "id": "m1", "text": text},
    }


def _settings(**over):
    s = {
        "channel_secret": SECRET, "channel_access_token": TOKEN,
        "app": {"app_id": "app-1"}, "mdtoflex": False,
        "img_variable_name": "img", "img_prompt": "Describe",
        "debug_mode": False, "pass_display_name": True, "display_name_variable": "displayName",
    }
    s.update(over)
    return s


@pytest.fixture
def env():
    """建立 endpoint、假 session、假 LineClient；回傳三者供測試斷言。"""
    storage = FakeStorage()
    invoke = MagicMock(return_value={"answer": "answer!", "conversation_id": "c1"})
    session = SimpleNamespace(
        storage=storage,
        app=SimpleNamespace(chat=SimpleNamespace(invoke=invoke)),
        file=SimpleNamespace(upload=MagicMock()),
    )
    line = MagicMock()
    line.get_profile.return_value = ("Kevin", None)
    line.get_quota.return_value = None
    ep = LineEndpoint.__new__(LineEndpoint)
    ep.session = session
    with patch("endpoints.linebot.LineClient", return_value=line):
        yield SimpleNamespace(ep=ep, session=session, line=line, invoke=invoke, storage=storage)


def test_invalid_signature_returns_400(env):
    resp = env.ep._invoke(_request({"events": []}, signature="bad"), {}, _settings())
    assert resp.status_code == 400


def test_verify_with_empty_events_returns_200_without_calling_dify(env):
    resp = env.ep._invoke(_request({"events": []}), {}, _settings())
    assert resp.status_code == 200
    env.invoke.assert_not_called()


def test_text_message_invokes_dify_and_replies(env):
    resp = env.ep._invoke(_request({"events": [_text_event("hello")]}), {}, _settings())
    assert resp.status_code == 200
    kwargs = env.invoke.call_args.kwargs
    assert kwargs["query"] == "hello"
    assert kwargs["inputs"]["user_id"] == "U1"
    assert kwargs["inputs"]["displayName"] == "Kevin"
    env.line.show_loading.assert_called_once()
    target, rt, msgs = env.line.send.call_args.args
    assert target == {"type": "user", "id": "U1"} and rt == "rt-1"
    assert msgs == [{"type": "text", "text": "answer!"}]
    # 對話 ID 已保存供下一輪使用
    assert env.storage.data[f"{SECRET}_U1"] == b"c1"


def test_duplicate_event_is_processed_once(env):
    body = {"events": [_text_event(event_id="same")]}
    env.ep._invoke(_request(body), {}, _settings())
    env.ep._invoke(_request(body), {}, _settings())
    assert env.invoke.call_count == 1


def test_clear_command_deletes_conversation_and_skips_dify(env):
    env.storage.set(f"{SECRET}_U1", b"old")
    env.ep._invoke(_request({"events": [_text_event("/clearconversationhistory")]}), {}, _settings())
    env.invoke.assert_not_called()
    assert f"{SECRET}_U1" not in env.storage.data
    assert "cleared" in env.line.send.call_args.args[2][0]["text"].lower()


def test_follow_event_sends_welcome_message(env):
    ev = {"type": "follow", "replyToken": "rt-f", "webhookEventId": "f1",
          "source": {"type": "user", "userId": "U1"}}
    env.ep._invoke(_request({"events": [ev]}), {}, _settings(welcome_message="Welcome!"))
    env.invoke.assert_not_called()
    assert env.line.send.call_args.args[2] == [{"type": "text", "text": "Welcome!"}]


def test_dify_timeout_reports_friendly_error_and_debug_detail(env):
    env.invoke.side_effect = DifyTimeoutError("Dify did not answer within 110s")
    env.ep._invoke(_request({"events": [_text_event()]}), {}, _settings())
    friendly = env.line.send.call_args.args[2][0]["text"]
    assert "110s" not in friendly
    env.ep._invoke(_request({"events": [_text_event(event_id="e2")]}), {}, _settings(debug_mode=True))
    detailed = env.line.send.call_args.args[2][0]["text"]
    assert "110s" in detailed


def test_group_message_uses_group_target_and_key(env):
    ev = _text_event(groupId="G9")
    ev["source"]["type"] = "group"
    env.ep._invoke(_request({"events": [ev]}), {}, _settings())
    target = env.line.send.call_args.args[0]
    assert target == {"type": "group", "id": "G9"}
    assert env.storage.data[f"{SECRET}_G9"] == b"c1"


def test_image_message_uploads_and_sends_file_input(env):
    env.line.download_content.return_value = b"jpeg"
    env.session.file.upload.return_value = SimpleNamespace(id="f-1", mime_type="image/jpeg", preview_url="https://f/1.jpg?s=1")
    ev = _text_event()
    ev["message"] = {"type": "image", "id": "m-img", "contentProvider": {"type": "line"}}
    env.ep._invoke(_request({"events": [ev]}), {}, _settings())
    kwargs = env.invoke.call_args.kwargs
    assert kwargs["query"] == "Describe"
    # 沒有 API 金鑰時走選項 B：外掛自己提供圖片網址（測試請求的 Host 為 localhost）
    (param,) = kwargs["inputs"]["img"]
    assert param["transfer_method"] == "remote_url" and param["type"] == "image"
    assert param["url"].startswith("https://localhost/img/")
    env.session.file.upload.assert_not_called()


def test_image_retries_with_single_file_when_variable_is_not_a_list(env):
    """Chatflow 的 img 變數若是單一檔案型別，Dify 會回 'must be a file'，應改以單一物件重送。"""
    env.line.download_content.return_value = b"jpeg"
    env.session.file.upload.return_value = SimpleNamespace(id="f-1", mime_type="image/jpeg", preview_url="https://f/1.jpg?s=1")
    env.invoke.side_effect = [
        Exception("invoke app failed: request failed with status code: 400 and respond with: "
                  '{"code":"invalid_param","message":"img in input form must be a file","status":400}'),
        {"answer": "a cat", "conversation_id": "c9"},
    ]
    ev = _text_event()
    ev["message"] = {"type": "image", "id": "m-img", "contentProvider": {"type": "line"}}
    env.ep._invoke(_request({"events": [ev]}), {}, _settings())
    assert env.invoke.call_count == 2
    first_param = env.invoke.call_args_list[0].kwargs["inputs"]["img"][0]
    second_inputs = env.invoke.call_args_list[1].kwargs["inputs"]
    # 第二次以單一物件重送，內容與第一次清單中的檔案參數相同
    assert second_inputs["img"] == first_param
    assert env.line.send.call_args.args[2] == [{"type": "text", "text": "a cat"}]


# ── 選項 B：無 API 金鑰時由外掛自己提供圖片網址 ───────────────

from endpoints.image_serve import ImageServeEndpoint  # noqa: E402


def _image_request(body: dict) -> Request:
    """帶 Dify-Hook-Url 標頭的 webhook 請求（Dify 轉送時會附上端點的公開網址）。"""
    raw = json.dumps(body)
    headers = {"X-Line-Signature": _sign(raw), "Dify-Hook-Url": "https://abc123.ai-plugin.io/"}
    return Request(EnvironBuilder(method="POST", data=raw, headers=headers,
                                  content_type="application/json").get_environ())


def test_image_without_api_key_is_served_by_plugin_public_url(env):
    env.line.download_content.return_value = b"jpeg-bytes"
    ev = _text_event()
    ev["message"] = {"type": "image", "id": "m-img", "contentProvider": {"type": "line"}}
    env.ep._invoke(_image_request({"events": [ev]}), {}, _settings(dify_api_key=""))
    # 不應動用 SDK 上傳，改用外掛自己的圖片端點
    env.session.file.upload.assert_not_called()
    file_param = env.invoke.call_args.kwargs["inputs"]["img"][0]
    assert file_param["transfer_method"] == "remote_url" and file_param["type"] == "image"
    assert file_param["url"].startswith("https://abc123.ai-plugin.io/img/")
    # Dify 回答後暫存已清除
    token = file_param["url"].rsplit("/", 1)[1]
    from core.storage import ImageCache
    assert ImageCache(env.storage, SECRET).get(token) is None


def test_image_serve_endpoint_returns_cached_image_and_404_for_bad_token(env):
    from core.storage import ImageCache
    token = ImageCache(env.storage, SECRET).put(b"jpeg-bytes", "image/jpeg")
    serve = ImageServeEndpoint.__new__(ImageServeEndpoint)
    serve.session = env.session
    req = Request(EnvironBuilder(method="GET", path=f"/img/{token}").get_environ())
    resp = serve._invoke(req, {"token": token}, _settings())
    assert resp.status_code == 200
    assert resp.content_type.startswith("image/jpeg")
    assert resp.get_data() == b"jpeg-bytes"
    bad = serve._invoke(req, {"token": "nope"}, _settings())
    assert bad.status_code == 404
