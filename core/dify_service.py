"""
Dify 端的服務：透過外掛 SDK 反向呼叫 Chatflow，以及上傳檔案。
"""
import logging
import traceback
from typing import Any, Dict, Optional, Tuple

import gevent
import requests
from urllib.parse import parse_qs, urlparse

from dify_plugin.core.entities.invocation import InvokeType
from dify_plugin.invocations.file import UploadFileResponse

from .constants import DIFY_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


class DifyTimeoutError(Exception):
    """Dify 在限定秒數內沒有回應。"""


class DifyChatService:
    """以 session.app.chat.invoke 呼叫使用者在端點設定中選擇的 App。"""

    def __init__(self, session, app_id: str, timeout: float = DIFY_TIMEOUT_SECONDS):
        self.session = session
        self.app_id = app_id
        self.timeout = timeout

    def chat(self, query: str, inputs: Dict[str, Any],
             conversation_id: Optional[str]) -> Tuple[str, Optional[str]]:
        """
        送一則訊息給 Dify App（blocking 模式），最多等待 self.timeout 秒。

        Returns:
            (answer, conversation_id)
        Raises:
            DifyTimeoutError: 超過逾時秒數
        """
        params: Dict[str, Any] = {
            "app_id": self.app_id,
            "query": query,
            "inputs": inputs,
            "response_mode": "blocking",
        }
        if conversation_id:
            params["conversation_id"] = conversation_id

        # SDK 已 monkey-patch gevent，gevent.Timeout 能中斷等待中的網路 I/O
        timer = gevent.Timeout(self.timeout, DifyTimeoutError(
            f"Dify did not answer within {self.timeout:g}s"))
        timer.start()
        try:
            response = self.session.app.chat.invoke(**params)
        finally:
            timer.cancel()
        return response.get("answer") or "", response.get("conversation_id")


class FileUploader:
    """
    負責把 LINE 收到的檔案上傳到 Dify，並轉成 Chatflow 檔案變數所需的參數格式。

    上傳策略：
    1. 優先使用外掛 SDK 的 session.file.upload（反向呼叫，不需 API 金鑰）。
    2. 若 SDK 上傳失敗且使用者有填 Dify API 金鑰，退回呼叫 /files/upload REST API。
    """

    def __init__(self, session=None, dify_base_url="https://api.dify.ai/v1", dify_api_key=None):
        self.session = session
        self.dify_base_url = dify_base_url or "https://api.dify.ai/v1"
        self.dify_api_key = dify_api_key

    @staticmethod
    def _to_app_parameter(file_id: str, mimetype: str, via_sdk: bool,
                          preview_url: Optional[str] = None) -> Dict[str, Any]:
        """
        把上傳結果組成 app.chat.invoke inputs 需要的檔案參數。

        三種引用方式（實測於 Dify Cloud 2026-09）：
        - REST /files/upload 的檔案在 upload_files 表 → local_file + upload_file_id。
        - SDK session.file.upload 的檔案在 tool_files 表，擁有者是 daemon 用的匿名 EndUser，
          而 App 執行時會換成該 App 專屬的 EndUser，tool_file 引用會因擁有者不符被拒
          （"ToolFile ... not found"）；local_file 則直接 "Invalid upload file"。
        - 因此 SDK 路徑優先用 Dify 回傳的簽名預覽網址以 remote_url 引用，Dify 會自行抓取，
          不經資料庫擁有者檢查；沒有預覽網址時才退回 tool_file。
        """
        file_type = UploadFileResponse.Type.from_mime_type(mimetype).value
        if not via_sdk:
            return {"upload_file_id": file_id, "transfer_method": "local_file", "type": file_type}
        if preview_url:
            return {"transfer_method": "remote_url", "url": preview_url, "type": file_type}
        return {"tool_file_id": file_id, "transfer_method": "tool_file", "type": file_type}

    def upload_file(self, filename: str, content: bytes, mimetype: str) -> Optional[Dict[str, Any]]:
        """
        上傳檔案到 Dify 並回傳 Chatflow 檔案參數；全部失敗回傳 None。

        順序：
        1. 有 API 金鑰時走 REST /files/upload，並帶 user=<daemon 呼叫 App 用的匿名 EndUser id>，
           讓上傳者與 App 執行者是同一個 EndUser，通過 Dify 的檔案擁有者檢查（Dify Cloud 實測必要）。
        2. 否則（或 REST 失敗）走 SDK session.file.upload；在有擁有者檢查的 Dify 上會被拒，
           但舊版／自架 Dify 仍可用。
        """
        if self.dify_api_key and self.session is not None:
            user_id = self._resolve_dify_user_id(filename, mimetype)
            if user_id:
                result = self.upload_file_via_api(filename, content, mimetype, user=user_id)
                if result:
                    return result
                logger.warning("REST upload as end user %s failed, trying SDK upload", user_id)
        result = self.upload_file_via_session(filename, content, mimetype)
        if result:
            return result
        if self.dify_api_key:
            logger.info("SDK upload failed, falling back to Dify REST API upload without user")
            return self.upload_file_via_api(filename, content, mimetype)
        logger.error("SDK upload failed and no Dify API key configured; file will be skipped")
        return None

    def _resolve_dify_user_id(self, filename: str, mimetype: str) -> Optional[str]:
        """
        取得 daemon 對本端點做反向呼叫時使用的 EndUser id。

        SDK 沒有直接提供，但「請求簽名上傳網址」的回應網址內含 user_id 查詢參數，
        而呼叫 App 時 Dify 會以同一個 id 對應（或建立）該 App 的 EndUser，
        所以用它當 REST 上傳的 user 就能讓檔案擁有者一致。
        """
        try:
            for response in self.session.file._backwards_invoke(
                InvokeType.UploadFile, dict, {"filename": filename, "mimetype": mimetype}
            ):
                url = response.get("url") or ""
                user_id = parse_qs(urlparse(url).query).get("user_id", [None])[0]
                if user_id:
                    return user_id
                break
        except Exception as e:
            logger.warning("Could not resolve Dify end user id from signed upload url: %s", e)
        return None

    def upload_file_via_session(self, filename: str, content: bytes, mimetype: str) -> Optional[Dict[str, Any]]:
        """透過外掛 SDK 的 session.file.upload 上傳檔案（不需 API 金鑰）。"""
        if self.session is None:
            return None
        try:
            storage_file = self.session.file.upload(filename=filename, content=content, mimetype=mimetype)
            if not storage_file:
                return None
            return self._to_app_parameter(storage_file.id, getattr(storage_file, "mime_type", None) or mimetype,
                                          via_sdk=True, preview_url=getattr(storage_file, "preview_url", None))
        except Exception as e:
            logger.warning("Error uploading file via session: %s", e)
            logger.debug(traceback.format_exc())
            return None

    def upload_file_via_api(self, filename: str, content: bytes, mimetype: str,
                            user: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        呼叫 Dify /files/upload REST API（需 App API 金鑰）。內容以 bytes 傳遞，不寫暫存檔。

        Args:
            user: 上傳者的 end user 識別；須與之後呼叫 App 的 EndUser 相同，檔案才可被 App 讀取
        """
        if not self.dify_base_url or not self.dify_api_key:
            return None
        try:
            upload_url = f"{self.dify_base_url.rstrip('/')}/files/upload"
            headers = {"Authorization": f"Bearer {self.dify_api_key}"}
            files = {"file": (filename, content, mimetype)}
            data = {"user": user} if user else None
            response = requests.post(upload_url, headers=headers, files=files, data=data, timeout=60)
            if response.status_code in (200, 201):
                result = response.json()
                if "id" in result:
                    return self._to_app_parameter(result["id"], result.get("mime_type", mimetype), via_sdk=False)
                logger.error("File upload API response missing id: %s", result)
            else:
                logger.error("File upload API error: %s, %s", response.status_code, response.text[:300])
            return None
        except Exception as e:
            logger.error("Error uploading file via API: %s", e)
            logger.error(traceback.format_exc())
            return None
