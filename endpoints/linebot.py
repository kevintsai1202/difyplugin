from typing import Any, Mapping, Optional, Dict
from werkzeug import Request, Response
from dify_plugin import Endpoint
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageMessage, ImageSendMessage, FlexSendMessage,
    StickerMessage, AudioMessage, VideoMessage
)
import traceback
import hmac
import hashlib
import base64
import logging
import re
from utils.file_uploader import FileUploader
from utils.flex_formatter import MdFlexFormatHelper

logger = logging.getLogger(__name__)


class LineEndpoint(Endpoint):
    bot_user_id: Optional[str] = None
    _file_uploader: Optional[FileUploader] = None
    _flex_formatter: Optional[MdFlexFormatHelper] = None

    def _invoke(self, request: Request, values: Mapping, settings: Mapping) -> Response:
        """
        調用端點處理給定的請求。
        """
        if not request:
            return Response(status=200, response="ok")

        signature = request.headers.get('X-Line-Signature')
        if not signature:
            return Response(status=200, response="ok")

        # 獲取請求體作為文本
        body = request.get_data(as_text=True)
        if not body:
            return Response(status=200, response="ok")
        # 獲取Dify plugin變數
        lineChannelSecret = settings.get('channel_secret')
        lineChannelAccessToken = settings.get('channel_access_token')
        if not (lineChannelSecret and lineChannelAccessToken):
            return Response(status=200, response="ok")

        # 使用 Channel Secret 生成 HMAC-SHA256 簽名
        hash = hmac.new(
            lineChannelSecret.encode('utf-8'),
            body.encode('utf-8'),
            hashlib.sha256
        ).digest()
        computed_signature = base64.b64encode(hash).decode('utf-8')
        # 比對簽名
        if signature != computed_signature:
            raise InvalidSignatureError("signature error")
        # 初始化 LINE Bot API
        handler = WebhookHandler(lineChannelSecret)
        line_bot_api = LineBotApi(lineChannelAccessToken)

        # 註冊 TextMessage Event
        @handler.add(MessageEvent, message=TextMessage)
        def handle_message(event):
            # Line 傳來的 Message
            user_id = event.source.user_id
            group_id = getattr(event.source, "group_id", None)
            room_id = getattr(event.source, "room_id", None)
            user_message = event.message.text

# Set key_to_check based on available identifiers
            if group_id is not None and group_id:
                key_to_check = lineChannelSecret+"_"+group_id
            elif room_id is not None and room_id:
                key_to_check = lineChannelSecret+"_"+room_id
            else:
                key_to_check = lineChannelSecret+"_"+user_id

            # 最高優先級檢查：系統指令
            # 增加對空字串的判斷，確保即使設定未儲存，預設指令也能生效
            reset_command = (settings.get('reset_command') or 'reset').strip()
            help_command = (settings.get('help_command') or 'help').strip()

            if len(reset_command) >= 5 and user_message.lower() == f"/{reset_command}":
                try:
                    self.session.storage.delete(key_to_check)
                except Exception:
                    pass  # 忽略錯誤，確保回覆
                
                reset_msg = settings.get('reset_message') or "SYSTEM: Session history in Dify cleared."
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=reset_msg)
                )
                return
            
            elif user_message.lower() == f"/{help_command}":
                default_help_msg = "Hello! I'm an AI assistant powered by Dify platform. In 1-on-1 chats, ask me directly. In groups, please @mention me. I support text and image messages. Use /help for this info, /reset to clear history."
                help_msg = settings.get('help_message') or default_help_msg
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=help_msg)
                )
                return
            # if group_id or room_id, check if bot is mentioned
            if group_id or room_id:
                try:
                    if not self.bot_user_id:
                        bot_info = line_bot_api.get_bot_info()
                        self.bot_user_id = bot_info.user_id
                        logger.info(f"Successfully fetched bot user ID: {self.bot_user_id}")

                    mention = getattr(event.message, 'mention', None)
                    if not mention:
                        return

                    is_mentioned = any(
                        mentionee.user_id == self.bot_user_id for mentionee in mention.mentionees
                    )

                    if not is_mentioned:
                        return
                    
                    # Clean the message by removing all mentions
                    user_message = re.sub(r'@\S+\s?', '', user_message).strip()
                    if not user_message:
                        return # Ignore mention-only messages

                except Exception as e:
                    logger.error(f"Error processing mention by user ID: {e}")
                    logger.error(traceback.format_exc())
                    return

            # Set key_to_check based on available identifiers
            if group_id is not None and group_id:
                key_to_check = lineChannelSecret+"_"+group_id
            elif room_id is not None and room_id:
                key_to_check = lineChannelSecret+"_"+room_id
            else:
                key_to_check = lineChannelSecret+"_"+user_id
            # logger.debug(f"key_to_check: {key_to_check}")
            conversation_id = None
            # logger.debug("user_id:"+user_id)
            # logger.debug("user_message:"+user_message)
            try:
                conversation_id = self.session.storage.get(key_to_check)
                # logger.debug("conversation_id:"+conversation_id.decode('utf-8'))
            except Exception as e:
                err = traceback.format_exc()
                # logger.debug(err)

            try:
                # 收集識別資訊

                identify_inputs = {
                    "user_id": user_id,
                    "group_id": group_id,
                    "room_id": room_id,
                }
                invoke_params = {
                    "app_id": settings["app"]["app_id"],
                    "query": user_message,
                    "inputs": identify_inputs,
                    "response_mode": "blocking"
                }
                if conversation_id is not None:
                    invoke_params['conversation_id'] = conversation_id.decode(
                        'utf-8')

                response = self.session.app.chat.invoke(**invoke_params)
                answer = response.get("answer")
                conversation_id = response.get("conversation_id")
                # logger.debug("conversation_id:"+conversation_id)
                if conversation_id:
                    self.session.storage.set(
                        key_to_check, conversation_id.encode('utf-8'))
                # md to flex
                if settings.get("mdtoflex") and (
                    re.search(r'\|.*\|.*\|', answer) or re.search(r'\[.*\]\(.*\)', answer) or '```' in answer or
                    re.search(r'\*\*.*?\*\*', answer) or re.search(r'^\s*[\*\-]\s', answer, re.MULTILINE)
                ):
                    logger.debug(
                        f"Converting markdown to FlexMessage: {answer[:100]}...")
                    try:
                        if not self._flex_formatter:
                            self._flex_formatter = MdFlexFormatHelper()
                        flex_message = self._flex_formatter.md_to_flex(answer)
                        logger.debug(f"Generated FlexMessage: {flex_message}")
                        line_bot_api.reply_message(
                            event.reply_token,
                            FlexSendMessage(
                                alt_text="FlexMessage",
                                contents=flex_message
                            )
                        )
                        logger.debug("FlexMessage sent successfully")
                    except Exception as e:
                        logger.error(
                            f"Error creating or sending FlexMessage: {e}")
                        logger.error(traceback.format_exc())
                        # Fallback to regular text message
                        line_bot_api.reply_message(
                            event.reply_token,
                            TextSendMessage(text=answer)
                        )
                else:
                    image_urls = re.findall(r'!\[.*?\]\((.*?)\)', answer)
                    if image_urls:
                        messages = [
                            ImageSendMessage(
                                original_content_url=url, preview_image_url=url)
                            for url in image_urls
                        ]
                        line_bot_api.reply_message(event.reply_token, messages)
                    else:
                        line_bot_api.reply_message(
                            event.reply_token,
                            TextSendMessage(text=answer)
                        )

                return Response(
                    status=200,
                    response="ok",
                    content_type="text/plain",
                )

            except Exception as e:
                err = traceback.format_exc()
                logger.error(f"An unexpected error occurred in handle_message: {err}")
                try:
                    error_msg = settings.get('error_message', 'Sorry, I encountered a problem, please try again later.')
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text=error_msg)
                    )
                except Exception as reply_e:
                    logger.error(f"Failed to send error message to user: {reply_e}")
                return Response(
                    status=500,
                    response=err,
                    content_type="text/plain",
                )

        @handler.add(MessageEvent, message=ImageMessage)
        def handle_image(event):
            try:
                logger.debug(
                    f"[LineEndpoint] handle_image triggered. user_id={event.source.user_id}, message_id={event.message.id}")
                user_id = event.source.user_id
                group_id = getattr(event.source, "group_id", None)
                room_id = getattr(event.source, "room_id", None)
                message_id = event.message.id
                img_variable_name = settings.get('img_variable_name')
                img_prompt = settings.get('img_prompt')
                dify_api_key = settings.get('dify_api_key')

                # 如果缺少任何必要的設置則停止
                if not (img_variable_name and img_prompt and dify_api_key):
                    return

                # 在群組或聊天室中，檢查圖片是否回覆給 Bot
                if group_id or room_id:
                    # 檢查是否為回覆訊息
                    reply_message = getattr(event.message, 'reply_message', None)
                    if reply_message:
                        # 如果是回覆訊息，檢查是否回覆給 Bot 自己
                        if not self.bot_user_id:
                            bot_info = line_bot_api.get_bot_info()
                            self.bot_user_id = bot_info.user_id
                            logger.info(f"Successfully fetched bot user ID: {self.bot_user_id}")
                        
                        # 如果不是回覆給 Bot，則忽略
                        if reply_message.user_id != self.bot_user_id:
                            logger.info(f"Image is a reply to another user, ignoring. Reply to: {reply_message.user_id}")
                            return
                    else:
                        # 如果不是回覆訊息，檢查是否有 @ 提及（圖片訊息本身不能包含提及，但可能在同一則訊息中）
                        # 由於圖片訊息無法包含文字，這裡直接忽略非回覆的圖片
                        logger.info("Image in group/room is not a reply to bot, ignoring")
                        return

                content = line_bot_api.get_message_content(message_id)
                raw_bytes = content.content
                logger.debug(f"handle_image: retrieved {len(raw_bytes)} bytes")
                
                if not self._file_uploader:
                    self._file_uploader = FileUploader(session=self.session)
                
                self._file_uploader.dify_api_key = dify_api_key
                self._file_uploader.dify_base_url = settings.get('dify_api_url')

                upload_resp = self._file_uploader.upload_file_via_api(
                    f"{message_id}.jpg", raw_bytes, "image/jpeg"
                )
                if not upload_resp:
                    logger.warning("Image upload via API failed.")
                    return

                file_param = {
                    "upload_file_id": upload_resp["id"],
                    "type": "image",
                    "transfer_method": "local_file",
                }
                
                dify_inputs = {
                    img_variable_name: [file_param],
                }

                if group_id is not None and group_id:
                    key_to_check = lineChannelSecret+"_"+group_id
                elif room_id is not None and room_id:
                    key_to_check = lineChannelSecret+"_"+room_id
                else:
                    key_to_check = lineChannelSecret+"_"+user_id
                
                conversation_id = None
                try:
                    conversation_id = self.session.storage.get(key_to_check)
                except Exception:
                    pass

                identify_inputs = {
                    "user_id": user_id,
                    "group_id": group_id,
                    "room_id": room_id,
                }
                
                merged_inputs = {**dify_inputs, **identify_inputs}
                invoke_params = {
                    "app_id": settings["app"]["app_id"],
                    "query": img_prompt,
                    "inputs": merged_inputs,
                    "response_mode": "blocking",
                }
                if conversation_id is not None:
                    invoke_params["conversation_id"] = conversation_id.decode('utf-8')
                
                response = self.session.app.chat.invoke(**invoke_params)
                answer = response.get("answer")
                conversation_id = response.get("conversation_id")
                
                if conversation_id:
                    self.session.storage.set(
                        key_to_check, conversation_id.encode('utf-8'))
                
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=answer)
                )
            except Exception as e:
                err = traceback.format_exc()
                logger.error(f"An unexpected error occurred in handle_image: {err}")
                try:
                    error_msg = settings.get('error_message', 'Sorry, I encountered a problem, please try again later.')
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text=error_msg)
                    )
                except Exception as reply_e:
                    logger.error(f"Failed to send error message to user: {reply_e}")
        @handler.add(MessageEvent, message=[StickerMessage, AudioMessage, VideoMessage])
        def handle_unsupported_message(event):
            """處理目前不支援的訊息類型，並給予禮貌性回覆。"""
            unsupported_type = event.message.type
            logger.info(f"Received unsupported message type: {unsupported_type}")
            
            reply_text = f"抱歉，我還看不懂「{unsupported_type}」類型的訊息，請用文字或圖片與我溝通喔！"
            
            try:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=reply_text)
                )
            except Exception as e:
                logger.error(f"Failed to reply to unsupported message type: {e}")

        # 處理 webhook
        try:
            handler.handle(body, signature)
            return Response(
                status=200,
                response="ok",
                content_type="text/plain",
            )
        except InvalidSignatureError:
            err = traceback.format_exc()
            return Response(
                status=400,
                response=err,
                content_type="text/plain",
            )
        except Exception as e:
            err = traceback.format_exc()
            return Response(
                status=500,
                response=err,
                content_type="text/plain",
            )


