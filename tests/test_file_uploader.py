"""
FileUploader.upload_file 的單元測試。

驗證重點：
1. 優先走 session.file.upload（SDK 反向呼叫），成功時不呼叫 Dify REST API。
2. session 失敗且有填 API 金鑰時，退回 REST API 路徑，且上傳內容以記憶體物件傳遞（不寫 /tmp）。
3. session 失敗且沒有 API 金鑰時回傳 None。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dify_plugin.invocations.file import UploadFileResponse

from core.dify_service import FileUploader

# 測試用的假圖片內容
CONTENT = b"\xff\xd8fake-jpeg"


def _session_ok():
    """建立會成功回傳 UploadFileResponse 的假 session。"""
    resp = UploadFileResponse(
        id="file-123", name="a.jpg", size=len(CONTENT), extension="jpg", mime_type="image/jpeg",
        preview_url="https://upload.dify.ai/files/tools/file-123.jpg?sign=abc",
    )
    return SimpleNamespace(file=SimpleNamespace(upload=MagicMock(return_value=resp)))


def _session_fail():
    """建立 upload 會拋錯的假 session（模擬舊版 Dify 不支援）。"""
    return SimpleNamespace(file=SimpleNamespace(upload=MagicMock(side_effect=RuntimeError("boom"))))


def test_upload_prefers_session_and_skips_api():
    uploader = FileUploader(session=_session_ok(), dify_api_key="should-not-be-used")
    with patch("core.dify_service.requests.post") as post:
        result = uploader.upload_file("a.jpg", CONTENT, "image/jpeg")
    # SDK 上傳的檔案存在 tool_files 表且擁有者是匿名 EndUser，App 以 tool_file 引用會因擁有者不符被拒；
    # 改用 Dify 回傳的簽名預覽網址以 remote_url 方式引用，不經擁有者檢查
    assert result == {"transfer_method": "remote_url",
                      "url": "https://upload.dify.ai/files/tools/file-123.jpg?sign=abc", "type": "image"}
    post.assert_not_called()


def test_upload_falls_back_to_tool_file_when_no_preview_url():
    resp = UploadFileResponse(id="file-9", name="a.jpg", size=1, extension="jpg", mime_type="image/jpeg")
    session = SimpleNamespace(file=SimpleNamespace(upload=MagicMock(return_value=resp)))
    result = FileUploader(session=session).upload_file("a.jpg", CONTENT, "image/jpeg")
    assert result == {"tool_file_id": "file-9", "transfer_method": "tool_file", "type": "image"}


def test_upload_falls_back_to_api_without_temp_file():
    uploader = FileUploader(
        session=_session_fail(), dify_base_url="https://api.dify.ai/v1", dify_api_key="key"
    )
    api_resp = MagicMock(status_code=201)
    api_resp.json.return_value = {"id": "api-456", "name": "a.jpg", "mime_type": "image/jpeg"}
    with patch("core.dify_service.requests.post", return_value=api_resp) as post, \
            patch("builtins.open") as opened:
        result = uploader.upload_file("a.jpg", CONTENT, "image/jpeg")
    assert result == {"upload_file_id": "api-456", "transfer_method": "local_file", "type": "image"}
    post.assert_called_once()
    assert post.call_args.args[0] == "https://api.dify.ai/v1/files/upload"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer key"}
    # 上傳內容必須直接以 bytes 傳入，不可落地寫檔
    _, payload, _ = post.call_args.kwargs["files"]["file"]
    assert payload == CONTENT
    opened.assert_not_called()


def test_upload_returns_none_when_session_fails_and_no_api_key():
    uploader = FileUploader(session=_session_fail(), dify_api_key=None)
    with patch("core.dify_service.requests.post") as post:
        assert uploader.upload_file("a.jpg", CONTENT, "image/jpeg") is None
    post.assert_not_called()


def test_upload_returns_none_when_api_rejects():
    uploader = FileUploader(session=_session_fail(), dify_api_key="key")
    api_resp = MagicMock(status_code=401, text="unauthorized")
    with patch("core.dify_service.requests.post", return_value=api_resp):
        assert uploader.upload_file("a.jpg", CONTENT, "image/jpeg") is None


def _session_with_signed_url(anon_user_id="anon-end-user-id"):
    """模擬 SDK 反向呼叫取得簽名上傳網址（網址內含 daemon 使用的匿名 EndUser id）。"""
    url = f"https://upload.dify.ai/files/upload/for-plugin?timestamp=1&nonce=n&sign=s&user_id={anon_user_id}&tenant_id=t"
    file_ns = SimpleNamespace(
        _backwards_invoke=MagicMock(return_value=iter([{"url": url}])),
        upload=MagicMock(side_effect=AssertionError("SDK upload must not be used when API key is set")),
    )
    return SimpleNamespace(file=file_ns)


def test_upload_with_api_key_uses_rest_api_as_the_same_end_user_dify_will_invoke_with():
    """
    Dify 的檔案擁有者檢查要求上傳者與呼叫 App 的 EndUser 相同。
    daemon 呼叫 App 用的是匿名 EndUser（其 id 出現在簽名上傳網址的 user_id），
    因此 REST 上傳必須帶 user=<該 id>，Dify 才會把兩者對到同一個 EndUser。
    """
    uploader = FileUploader(session=_session_with_signed_url("anon-1"), dify_api_key="key",
                            dify_base_url="https://api.dify.ai/v1")
    api_resp = MagicMock(status_code=201)
    api_resp.json.return_value = {"id": "up-1", "mime_type": "image/jpeg"}
    with patch("core.dify_service.requests.post", return_value=api_resp) as post:
        result = uploader.upload_file("a.jpg", CONTENT, "image/jpeg")
    assert result == {"upload_file_id": "up-1", "transfer_method": "local_file", "type": "image"}
    post.assert_called_once()
    assert post.call_args.args[0] == "https://api.dify.ai/v1/files/upload"
    assert post.call_args.kwargs["data"] == {"user": "anon-1"}


def test_upload_with_api_key_falls_back_to_sdk_when_user_id_cannot_be_resolved():
    file_ns = SimpleNamespace(
        _backwards_invoke=MagicMock(side_effect=RuntimeError("no daemon")),
        upload=MagicMock(return_value=UploadFileResponse(
            id="f-2", name="a.jpg", size=1, extension="jpg", mime_type="image/jpeg", preview_url="https://p/2.jpg")),
    )
    uploader = FileUploader(session=SimpleNamespace(file=file_ns), dify_api_key="key")
    with patch("core.dify_service.requests.post") as post:
        result = uploader.upload_file("a.jpg", CONTENT, "image/jpeg")
    assert result == {"transfer_method": "remote_url", "url": "https://p/2.jpg", "type": "image"}
    post.assert_not_called()
