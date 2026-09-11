"""
session.storage 的封裝：對話 ID 持久化、webhook 事件去重、圖片暫存。

儲存層是簡單的 key-value（值為 bytes），任何錯誤都不往外拋，
因為儲存失敗不應讓使用者收不到回覆。
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional, Tuple

from .constants import DEDUP_MAX_ITEMS, IMAGE_CACHE_TTL_SECONDS

logger = logging.getLogger(__name__)


def conversation_key(channel_secret: str, source: dict) -> str:
    """
    對話 ID 的儲存 key。群組 > 聊天室 > 使用者。
    格式沿用 0.0.5 版（channel_secret + "_" + id），升級後既有對話才能延續。
    """
    target = source.get("groupId") or source.get("roomId") or source.get("userId")
    return f"{channel_secret}_{target}"


class ConversationStore:
    """每個 LINE 對象（使用者／群組／聊天室）對應一個 Dify conversation_id。"""

    def __init__(self, storage, channel_secret: str):
        self.storage = storage
        self.secret = channel_secret

    def key(self, source: dict) -> str:
        return conversation_key(self.secret, source)

    def get_conversation_id(self, source: dict) -> Optional[str]:
        try:
            value = self.storage.get(self.key(source))
            return value.decode("utf-8") if value else None
        except Exception:
            return None

    def set_conversation_id(self, source: dict, conversation_id: str) -> None:
        try:
            self.storage.set(self.key(source), conversation_id.encode("utf-8"))
        except Exception as e:
            logger.warning("Failed to save conversation id: %s", e)

    def clear_conversation(self, source: dict) -> None:
        try:
            self.storage.delete(self.key(source))
        except Exception as e:
            logger.warning("Failed to clear conversation id: %s", e)


class EventDeduper:
    """
    以 webhookEventId 去重，避免 LINE 重送同一事件時重複呼叫 Dify。
    清單存在 storage 內，只保留最近 max_items 筆。
    """

    def __init__(self, storage, channel_secret: str, max_items: int = DEDUP_MAX_ITEMS):
        self.storage = storage
        # 以 secret 雜湊當命名空間，避免不同端點互相干擾，也不把 secret 寫進 key
        self.key = f"{hashlib.sha256(channel_secret.encode('utf-8')).hexdigest()[:8]}_dedup"
        self.max_items = max_items

    def is_duplicate(self, event_id: Optional[str]) -> bool:
        """已處理過回傳 True；否則記錄後回傳 False。沒有 ID 的事件一律視為新事件。"""
        if not event_id:
            return False
        try:
            raw = self.storage.get(self.key)
            seen = json.loads(raw.decode("utf-8")) if raw else []
        except Exception:
            seen = []
        if event_id in seen:
            logger.info("Duplicate webhook event %s skipped", event_id)
            return True
        seen.append(event_id)
        seen = seen[-self.max_items:]
        try:
            self.storage.set(self.key, json.dumps(seen).encode("utf-8"))
        except Exception as e:
            logger.warning("Failed to save dedup list: %s", e)
        return False


class ImageCache:
    """
    暫存 LINE 圖片，讓 Dify 能以外掛自己的公開網址抓取（不需 Dify API 金鑰）。

    token 格式：<nonce>.<expiry>.<sig>，sig 為以 channel_secret 對 nonce 與 expiry 做的 HMAC，
    因此 token 無法被猜出或竄改，且有時效；圖片內容存在 storage，key 只含 nonce。
    """

    def __init__(self, storage, channel_secret: str, ttl_seconds: int = IMAGE_CACHE_TTL_SECONDS):
        self.storage = storage
        self.secret = channel_secret.encode("utf-8")
        self.ns = hashlib.sha256(self.secret).hexdigest()[:8]
        self.ttl = ttl_seconds

    def _sign(self, nonce: str, expiry: str) -> str:
        return hmac.new(self.secret, f"{nonce}.{expiry}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def _key(self, nonce: str) -> str:
        return f"{self.ns}_img_{nonce}"

    def _parse(self, token: str) -> Optional[str]:
        """驗證 token 的簽名與時效；有效時回傳 nonce，否則 None。"""
        try:
            nonce, expiry, sig = token.split(".")
        except (ValueError, AttributeError):
            return None
        if not hmac.compare_digest(self._sign(nonce, expiry), sig):
            return None
        try:
            if time.time() > int(expiry):
                return None
        except ValueError:
            return None
        return nonce

    def put(self, content: bytes, mimetype: str) -> str:
        """存入圖片並回傳可放進網址的 token。"""
        nonce = secrets.token_hex(16)
        expiry = str(int(time.time()) + self.ttl)
        token = f"{nonce}.{expiry}.{self._sign(nonce, expiry)}"
        # 內容格式：mimetype + 換行 + 二進位內容
        self.storage.set(self._key(nonce), mimetype.encode("utf-8") + b"\n" + content)
        return token

    def get(self, token: str) -> Optional[Tuple[bytes, str]]:
        """以 token 取回 (內容, mimetype)；token 無效、過期或已刪除時回傳 None。"""
        nonce = self._parse(token)
        if not nonce:
            return None
        try:
            raw = self.storage.get(self._key(nonce))
        except Exception:
            return None
        if not raw:
            return None
        mimetype, _, content = raw.partition(b"\n")
        return content, mimetype.decode("utf-8", "replace")

    def delete(self, token: str) -> None:
        nonce = self._parse(token) or (token.split(".")[0] if token else None)
        if not nonce:
            return
        try:
            self.storage.delete(self._key(nonce))
        except Exception as e:
            logger.debug("Failed to delete cached image: %s", e)
