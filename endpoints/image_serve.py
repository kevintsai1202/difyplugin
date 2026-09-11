"""
圖片提供端點：GET /img/<token>

LINE 傳來的圖片會先暫存於 session storage（core.storage.ImageCache），
再把本端點的公開網址以 remote_url 交給 Dify 抓取。token 含簽名與時效，
無法被猜出；抓取完成後由 webhook 端點刪除暫存。
"""
import logging
from typing import Mapping

from werkzeug import Request, Response
from dify_plugin import Endpoint

from core.storage import ImageCache

logger = logging.getLogger(__name__)


class ImageServeEndpoint(Endpoint):
    """依 token 回傳暫存的圖片內容。"""

    def _invoke(self, request: Request, values: Mapping, settings: Mapping) -> Response:
        token = values.get("token") if values else None
        secret = settings.get("channel_secret") or ""
        cached = ImageCache(self.session.storage, secret).get(token) if (token and secret) else None
        if not cached:
            return Response(status=404, response="not found", content_type="text/plain")
        content, mimetype = cached
        return Response(
            status=200,
            response=content,
            content_type=mimetype,
            headers={"Content-Length": str(len(content)), "Cache-Control": "private, no-store"},
        )
