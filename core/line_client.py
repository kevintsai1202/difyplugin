"""
LINE Messaging API 封裝。

以 requests 直接呼叫 REST API，不依賴已棄用的 line-bot-sdk v1 介面。
所有方法都不會把網路錯誤往外拋（除了 download_content），
因為送訊息失敗時外掛仍必須對 LINE webhook 回 200。
"""
import base64
import hashlib
import hmac
import logging
from typing import Dict, List, Optional, Tuple

import requests

from .constants import (
    LINE_API, LINE_DATA_API, LINE_MAX_MESSAGES, LINE_HTTP_TIMEOUT, LINE_DOWNLOAD_TIMEOUT,
)

logger = logging.getLogger(__name__)


def verify_signature(channel_secret: str, body: str, signature: str) -> bool:
    """模組層級的簽名驗證函式，方便在 controller 直接匯入使用。"""
    return LineClient.verify_signature(channel_secret, body, signature)


def push_target(source: dict) -> dict:
    """由 webhook 事件的 source 決定 push 的目標（群組 > 聊天室 > 使用者）。"""
    if source.get("groupId"):
        return {"type": "group", "id": source["groupId"]}
    if source.get("roomId"):
        return {"type": "room", "id": source["roomId"]}
    return {"type": "user", "id": source.get("userId")}


class LineClient:
    """對單一 LINE 官方帳號（一組 channel access token）的 API 客戶端。"""

    def __init__(self, channel_access_token: str):
        self.token = channel_access_token
        # 所有請求共用的授權標頭
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {channel_access_token}",
        }

    # ── 簽名 ──────────────────────────────────────────

    @staticmethod
    def sign(channel_secret: str, body: str) -> str:
        """依 LINE 規範計算 body 的 HMAC-SHA256 簽名（base64）。"""
        digest = hmac.new(channel_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    @staticmethod
    def verify_signature(channel_secret: str, body: str, signature: str) -> bool:
        """以常數時間比對驗證 X-Line-Signature，避免時序攻擊。"""
        if not signature:
            return False
        return hmac.compare_digest(LineClient.sign(channel_secret, body), signature)

    # ── 送訊息 ────────────────────────────────────────

    def send(self, target: dict, reply_token: Optional[str], messages: List[dict]) -> bool:
        """
        送出訊息：有 reply token 就先用 reply（免費），失敗（例如 token 過期）再改 push。

        Returns:
            是否成功送出
        """
        messages = messages[:LINE_MAX_MESSAGES]
        if reply_token and self.reply(reply_token, messages):
            return True
        return self.push(target["id"], messages)

    def reply(self, reply_token: str, messages: List[dict]) -> bool:
        """以 reply token 回覆；LINE 回非 200 時回傳 False 讓呼叫端改用 push。"""
        try:
            resp = requests.post(
                f"{LINE_API}/bot/message/reply", headers=self._headers,
                json={"replyToken": reply_token, "messages": messages}, timeout=LINE_HTTP_TIMEOUT,
            )
            if resp.status_code == 200:
                return True
            logger.warning("LINE reply failed %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("LINE reply error: %s", e)
        return False

    def push(self, to: str, messages: List[dict]) -> bool:
        """主動推送訊息（計入官方帳號每月額度）。"""
        try:
            resp = requests.post(
                f"{LINE_API}/bot/message/push", headers=self._headers,
                json={"to": to, "messages": messages}, timeout=LINE_HTTP_TIMEOUT,
            )
            if resp.status_code == 200:
                return True
            logger.error("LINE push failed %s: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("LINE push error: %s", e)
        return False

    # ── 輔助 API ──────────────────────────────────────

    def show_loading(self, chat_id: str, seconds: int = 20) -> None:
        """顯示「正在輸入」載入動畫；只對一對一聊天有效，任何錯誤都忽略。"""
        try:
            resp = requests.post(
                f"{LINE_API}/bot/chat/loading/start", headers=self._headers,
                json={"chatId": chat_id, "loadingSeconds": seconds}, timeout=5,
            )
            if resp.status_code not in (200, 202):
                logger.debug("LINE loading %s: %s", resp.status_code, resp.text[:100])
        except Exception as e:
            logger.debug("LINE loading error: %s", e)

    def get_quota(self) -> Optional[Dict]:
        """
        查詢本月訊息額度。

        Returns:
            {"type": "limited"|"none", "limit": int|None, "used": int, "remaining": int|None}
            API 失敗時回傳 None。type 為 none 表示無上限，remaining 為 None。
        """
        try:
            q = requests.get(f"{LINE_API}/bot/message/quota", headers=self._headers, timeout=LINE_HTTP_TIMEOUT)
            if q.status_code != 200:
                logger.warning("LINE quota failed %s: %s", q.status_code, q.text[:200])
                return None
            c = requests.get(f"{LINE_API}/bot/message/quota/consumption",
                             headers=self._headers, timeout=LINE_HTTP_TIMEOUT)
            if c.status_code != 200:
                logger.warning("LINE quota consumption failed %s: %s", c.status_code, c.text[:200])
                return None
            quota, usage = q.json(), c.json()
            limit = quota.get("value") if quota.get("type") == "limited" else None
            used = int(usage.get("totalUsage", 0))
            return {
                "type": quota.get("type"),
                "limit": limit,
                "used": used,
                "remaining": (limit - used) if limit is not None else None,
            }
        except Exception as e:
            logger.warning("LINE quota error: %s", e)
            return None

    def get_profile(self, source: dict) -> Tuple[Optional[str], Optional[str]]:
        """取得使用者顯示名稱與頭像網址；群組與聊天室要走成員專用端點。"""
        uid = source.get("userId")
        if not uid:
            return None, None
        if source.get("groupId"):
            url = f"{LINE_API}/bot/group/{source['groupId']}/member/{uid}"
        elif source.get("roomId"):
            url = f"{LINE_API}/bot/room/{source['roomId']}/member/{uid}"
        else:
            url = f"{LINE_API}/bot/profile/{uid}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("displayName"), data.get("pictureUrl")
        except Exception as e:
            logger.debug("LINE profile error: %s", e)
        return None, None

    def download_content(self, message_id: str) -> bytes:
        """下載使用者傳來的圖片或檔案內容；失敗時拋出例外由呼叫端處理。"""
        resp = requests.get(
            f"{LINE_DATA_API}/bot/message/{message_id}/content",
            headers={"Authorization": f"Bearer {self.token}"}, timeout=LINE_DOWNLOAD_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.content
