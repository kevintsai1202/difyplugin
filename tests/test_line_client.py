"""
LineClient 的單元測試：簽名驗證、reply 失敗改 push、載入動畫、額度查詢、內容下載。
所有 HTTP 呼叫都以 patch requests 取代，不會真的連到 LINE。
"""
import base64
import hashlib
import hmac
from unittest.mock import MagicMock, patch


from core.line_client import LineClient

SECRET = "test-secret"
TOKEN = "test-token"


def _resp(status=200, json_data=None, text=""):
    """建立假的 requests.Response。"""
    r = MagicMock(status_code=status, text=text)
    r.json.return_value = json_data if json_data is not None else {}
    return r


# ── 簽名 ──────────────────────────────────────────────

def test_verify_signature_accepts_valid_signature():
    body = '{"events":[]}'
    sig = base64.b64encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()).decode()
    assert LineClient.verify_signature(SECRET, body, sig) is True


def test_verify_signature_rejects_tampered_body():
    body = '{"events":[]}'
    sig = base64.b64encode(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()).decode()
    assert LineClient.verify_signature(SECRET, body + " ", sig) is False


def test_verify_signature_rejects_empty_signature():
    assert LineClient.verify_signature(SECRET, "{}", "") is False


# ── 送訊息：reply 優先，失敗改 push ─────────────────────

def test_send_uses_reply_when_reply_succeeds():
    client = LineClient(TOKEN)
    with patch("core.line_client.requests.post", return_value=_resp(200)) as post:
        client.send({"type": "user", "id": "U1"}, "rt-1", [{"type": "text", "text": "hi"}])
    post.assert_called_once()
    assert post.call_args.args[0].endswith("/bot/message/reply")
    assert post.call_args.kwargs["json"]["replyToken"] == "rt-1"


def test_send_falls_back_to_push_when_reply_token_expired():
    client = LineClient(TOKEN)
    responses = [_resp(400, text="Invalid reply token"), _resp(200)]
    with patch("core.line_client.requests.post", side_effect=responses) as post:
        client.send({"type": "user", "id": "U1"}, "rt-expired", [{"type": "text", "text": "hi"}])
    assert post.call_count == 2
    assert post.call_args_list[1].args[0].endswith("/bot/message/push")
    assert post.call_args_list[1].kwargs["json"]["to"] == "U1"


def test_send_pushes_directly_when_no_reply_token():
    client = LineClient(TOKEN)
    with patch("core.line_client.requests.post", return_value=_resp(200)) as post:
        client.send({"type": "group", "id": "G1"}, None, [{"type": "text", "text": "hi"}])
    post.assert_called_once()
    assert post.call_args.args[0].endswith("/bot/message/push")
    assert post.call_args.kwargs["json"]["to"] == "G1"


def test_send_caps_messages_at_line_limit_of_five():
    client = LineClient(TOKEN)
    msgs = [{"type": "text", "text": str(i)} for i in range(7)]
    with patch("core.line_client.requests.post", return_value=_resp(200)) as post:
        client.send({"type": "user", "id": "U1"}, "rt", msgs)
    assert len(post.call_args.kwargs["json"]["messages"]) == 5


# ── 載入動畫 ──────────────────────────────────────────

def test_show_loading_posts_chat_id_and_seconds_and_swallows_errors():
    client = LineClient(TOKEN)
    with patch("core.line_client.requests.post", return_value=_resp(202)) as post:
        client.show_loading("U1", seconds=30)
    assert post.call_args.args[0].endswith("/bot/chat/loading/start")
    assert post.call_args.kwargs["json"] == {"chatId": "U1", "loadingSeconds": 30}
    # 網路錯誤不得往外拋，載入動畫只是輔助
    with patch("core.line_client.requests.post", side_effect=ConnectionError("down")):
        client.show_loading("U1")


# ── 額度查詢 ──────────────────────────────────────────

def test_get_quota_returns_remaining_for_limited_plan():
    client = LineClient(TOKEN)
    responses = [_resp(200, {"type": "limited", "value": 200}), _resp(200, {"totalUsage": 150})]
    with patch("core.line_client.requests.get", side_effect=responses):
        quota = client.get_quota()
    assert quota == {"type": "limited", "limit": 200, "used": 150, "remaining": 50}


def test_get_quota_returns_none_remaining_for_unlimited_plan():
    client = LineClient(TOKEN)
    responses = [_resp(200, {"type": "none"}), _resp(200, {"totalUsage": 3})]
    with patch("core.line_client.requests.get", side_effect=responses):
        quota = client.get_quota()
    assert quota["type"] == "none"
    assert quota["remaining"] is None


def test_get_quota_returns_none_on_api_error():
    client = LineClient(TOKEN)
    with patch("core.line_client.requests.get", return_value=_resp(401, text="bad token")):
        assert client.get_quota() is None


# ── 個人資料與內容下載 ────────────────────────────────

def test_get_profile_uses_group_member_endpoint_in_group():
    client = LineClient(TOKEN)
    with patch("core.line_client.requests.get",
               return_value=_resp(200, {"displayName": "Kevin", "pictureUrl": "p"})) as get:
        name, pic = client.get_profile({"userId": "U1", "groupId": "G1"})
    assert (name, pic) == ("Kevin", "p")
    assert get.call_args.args[0].endswith("/bot/group/G1/member/U1")


def test_download_content_uses_data_api_and_returns_bytes():
    client = LineClient(TOKEN)
    r = _resp(200)
    r.content = b"jpeg"
    with patch("core.line_client.requests.get", return_value=r) as get:
        assert client.download_content("M1") == b"jpeg"
    assert get.call_args.args[0] == "https://api-data.line.me/v2/bot/message/M1/content"
