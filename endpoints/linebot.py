"""
LINE webhook 端點（controller）。

流程：
  1. 驗證 X-Line-Signature（常數時間比對）
  2. 解析 events；Verify 按鈕送的是空陣列，直接回 200
  3. 逐一事件：去重 → 依類型分派（follow / text / image）
  4. 文字與圖片：顯示載入動畫 → 呼叫 Dify（110 秒逾時）→ 轉成 LINE 訊息 → reply，失敗改 push
  5. 任何錯誤都記錄日誌並告知使用者；debug_mode 開啟時附上錯誤細節
  6. 對 LINE 一律回 200，避免不必要的重送

實際邏輯在 core/ 各服務，本檔只做調度。
"""
import json
import logging
import traceback
from typing import Any, Dict, Mapping, Optional

from werkzeug import Request, Response
from dify_plugin import Endpoint

from core.constants import CLEAR_COMMAND, IMAGE_SERVE_PATH, LOADING_SECONDS_TEXT, LOADING_SECONDS_IMAGE
from core.dify_service import DifyChatService, DifyTimeoutError, FileUploader
from core.formatting import build_messages, text_message
from core.line_client import LineClient, push_target, verify_signature
from core.storage import ConversationStore, EventDeduper, ImageCache

logger = logging.getLogger(__name__)

# 對使用者顯示的友善錯誤訊息
MSG_TIMEOUT = "Sorry, the AI took too long to respond. Please try again."
MSG_ERROR = "Sorry, something went wrong. Please try again later."
MSG_CLEARED = "SYSTEM: Session history in Dify cleared."
MSG_IMAGE_FAILED = "Sorry, I couldn't process the image."


def _ok() -> Response:
    return Response(status=200, response="ok", content_type="text/plain")


class LineEndpoint(Endpoint):
    """接收 LINE webhook，把訊息轉給 Dify App 並回覆。"""

    def _invoke(self, request: Request, values: Mapping, settings: Mapping) -> Response:
        if not request:
            return _ok()
        body = request.get_data(as_text=True)
        signature = request.headers.get("X-Line-Signature", "")
        secret = settings.get("channel_secret") or ""
        token = settings.get("channel_access_token") or ""

        if not (secret and token):
            logger.error("channel_secret or channel_access_token is not configured")
            return _ok()
        if not verify_signature(secret, body, signature):
            logger.warning("Invalid LINE signature")
            return Response(status=400, response="invalid signature", content_type="text/plain")

        try:
            events = json.loads(body).get("events", [])
        except (json.JSONDecodeError, AttributeError):
            return Response(status=400, response="invalid json", content_type="text/plain")
        if not events:
            # LINE Console 的 Verify 按鈕會送空事件
            return _ok()

        line = LineClient(token)
        deduper = EventDeduper(self.session.storage, secret)
        for event in events:
            if deduper.is_duplicate(event.get("webhookEventId")):
                continue
            try:
                self._dispatch(event, line, settings, request)
            except Exception:
                logger.error("Unhandled error while processing LINE event:\n%s", traceback.format_exc())
                self._notify_error(event, line, settings, MSG_ERROR, traceback.format_exc())
        return _ok()

    # ── 分派 ─────────────────────────────────────────

    def _dispatch(self, event: dict, line: LineClient, settings: Mapping, request: Request) -> None:
        etype = event.get("type")
        if etype == "follow":
            welcome = settings.get("welcome_message")
            if welcome and event.get("replyToken"):
                line.send(push_target(event.get("source", {})), event["replyToken"], [text_message(welcome)])
            return
        if etype != "message":
            return
        mtype = event.get("message", {}).get("type")
        logger.info("LINE event type=%s message=%s source=%s", etype, mtype, event.get("source", {}).get("type"))
        if mtype == "text":
            self._on_text(event, line, settings)
        elif mtype == "image":
            self._on_image(event, line, settings, request)
        # 貼圖、影片、位置等其他類型目前不處理

    # ── 文字訊息 ─────────────────────────────────────

    def _on_text(self, event: dict, line: LineClient, settings: Mapping) -> None:
        source = event.get("source", {})
        target = push_target(source)
        reply_token = event.get("replyToken")
        text = event["message"].get("text", "")
        store = ConversationStore(self.session.storage, settings["channel_secret"])

        if text.strip().lower() == CLEAR_COMMAND:
            store.clear_conversation(source)
            line.send(target, reply_token, [text_message(MSG_CLEARED)])
            return

        line.show_loading(target["id"], LOADING_SECONDS_TEXT)
        inputs = self._build_inputs(source, line, settings)
        self._ask_dify_and_reply(text, inputs, source, target, reply_token, line, settings, store)

    # ── 圖片訊息 ─────────────────────────────────────

    def _on_image(self, event: dict, line: LineClient, settings: Mapping, request: Request) -> None:
        img_variable = settings.get("img_variable_name")
        img_prompt = settings.get("img_prompt")
        if not (img_variable and img_prompt):
            # 未設定圖片變數就不處理圖片
            return
        source = event.get("source", {})
        target = push_target(source)
        reply_token = event.get("replyToken")
        message_id = event["message"].get("id")
        secret = settings["channel_secret"]
        store = ConversationStore(self.session.storage, secret)

        line.show_loading(target["id"], LOADING_SECONDS_IMAGE)
        try:
            content = line.download_content(message_id)
        except Exception as e:
            logger.error("Failed to download LINE image %s: %s", message_id, e)
            self._notify_error(event, line, settings, MSG_IMAGE_FAILED, str(e))
            return

        # 取得檔案參數：
        #   A. 有 Dify API 金鑰 → REST 上傳（以 Dify 執行 App 的同一個 EndUser 身分），檔案進入 Dify
        #   B. 無金鑰 → 暫存圖片，由外掛自己的 /img/<token> 端點提供公開網址讓 Dify 抓取
        #   C. 以上皆不可用 → SDK 上傳（僅舊版／自架 Dify 可用）
        cache_token: Optional[str] = None
        file_param: Optional[Dict[str, Any]] = None
        if settings.get("dify_api_key"):
            file_param = FileUploader(session=self.session, dify_api_key=settings.get("dify_api_key"),
                                      dify_base_url=settings.get("dify_api_url")
                                      ).upload_file(f"{message_id}.jpg", content, "image/jpeg")
        if not file_param:
            public_url = self._public_image_url(request, secret, content, "image/jpeg")
            if public_url:
                cache_token = public_url.rsplit("/", 1)[1]
                file_param = {"transfer_method": "remote_url", "url": public_url, "type": "image"}
        if not file_param:
            file_param = FileUploader(session=self.session).upload_file(f"{message_id}.jpg", content, "image/jpeg")
        if not file_param:
            self._notify_error(event, line, settings, MSG_IMAGE_FAILED, "upload to Dify failed")
            return
        logger.info("Image %s passed to Dify as %s", message_id, file_param)

        inputs = self._build_inputs(source, line, settings)
        # 先以檔案清單送出（相容 README 範例的 file-list 變數）；
        # 若 Chatflow 把變數設成單一檔案，Dify 會回 "must be a file"，改以單一物件重送
        inputs[img_variable] = [file_param]
        try:
            self._ask_dify_and_reply(img_prompt, inputs, source, target, reply_token, line, settings, store,
                                     retry_inputs={**inputs, img_variable: file_param},
                                     retry_when="must be a file")
        finally:
            if cache_token:
                ImageCache(self.session.storage, secret).delete(cache_token)

    def _public_image_url(self, request: Request, secret: str, content: bytes, mimetype: str) -> Optional[str]:
        """
        把圖片暫存並組出外掛自己的公開圖片網址。
        Dify 轉送 webhook 時會帶 Dify-Hook-Url 標頭（本端點的公開網址），沒有時退回 Host 標頭。
        """
        base = (request.headers.get("Dify-Hook-Url") or "").strip()
        if not base and request.headers.get("Host"):
            base = f"https://{request.headers['Host']}/"
        if not base:
            logger.warning("Cannot determine public endpoint URL; skipping plugin-served image")
            return None
        try:
            token = ImageCache(self.session.storage, secret).put(content, mimetype)
        except Exception as e:
            logger.warning("Failed to cache image for public serving: %s", e)
            return None
        return f"{base.rstrip('/')}{IMAGE_SERVE_PATH}/{token}"

    # ── 共用 ─────────────────────────────────────────

    def _build_inputs(self, source: dict, line: LineClient, settings: Mapping) -> Dict[str, Any]:
        """組 Dify inputs：LINE 識別資訊，視設定加上顯示名稱。"""
        inputs: Dict[str, Any] = {
            "user_id": source.get("userId"),
            "group_id": source.get("groupId"),
            "room_id": source.get("roomId"),
        }
        if settings.get("pass_display_name", True):
            name, _ = line.get_profile(source)
            var = settings.get("display_name_variable") or "displayName"
            inputs[var] = name or "LINE User"
        return inputs

    def _ask_dify_and_reply(self, query: str, inputs: Dict[str, Any], source: dict, target: dict,
                            reply_token: Optional[str], line: LineClient, settings: Mapping,
                            store: ConversationStore, retry_inputs: Optional[Dict[str, Any]] = None,
                            retry_when: str = "") -> None:
        """
        呼叫 Dify、保存對話 ID、把回答送回 LINE。錯誤轉成友善訊息。

        Args:
            retry_inputs: Dify 回傳的錯誤訊息含 retry_when 字串時，改用這組 inputs 重送一次
        """
        service = DifyChatService(self.session, app_id=settings["app"]["app_id"])
        conversation_id = store.get_conversation_id(source)
        try:
            try:
                answer, new_cid = service.chat(query, inputs, conversation_id)
            except DifyTimeoutError:
                raise
            except Exception as e:
                if retry_inputs is None or retry_when not in str(e):
                    raise
                logger.info("Dify rejected inputs (%s); retrying with alternate file shape", retry_when)
                answer, new_cid = service.chat(query, retry_inputs, conversation_id)
        except DifyTimeoutError as e:
            logger.error("Dify timeout: %s", e)
            self._send_error(target, reply_token, line, settings, MSG_TIMEOUT, str(e))
            return
        except Exception as e:
            logger.error("Dify invoke failed:\n%s", traceback.format_exc())
            self._send_error(target, reply_token, line, settings, MSG_ERROR, str(e))
            return

        logger.info("Dify answered %d chars, conversation_id=%s", len(answer), new_cid)
        if new_cid:
            store.set_conversation_id(source, new_cid)
        sent = line.send(target, reply_token, build_messages(answer, bool(settings.get("mdtoflex"))))
        logger.info("LINE send %s", "ok" if sent else "FAILED")

    def _notify_error(self, event: dict, line: LineClient, settings: Mapping,
                      friendly: str, detail: str) -> None:
        self._send_error(push_target(event.get("source", {})), event.get("replyToken"),
                         line, settings, friendly, detail)

    @staticmethod
    def _send_error(target: dict, reply_token: Optional[str], line: LineClient, settings: Mapping,
                    friendly: str, detail: str) -> None:
        """debug_mode 開啟時把錯誤細節回給 LINE 使用者，否則只回友善訊息。"""
        text = f"[DEBUG] {detail}"[-1000:] if settings.get("debug_mode") else friendly
        try:
            line.send(target, reply_token, [text_message(text)])
        except Exception as e:
            logger.error("Failed to send error message to LINE: %s", e)
