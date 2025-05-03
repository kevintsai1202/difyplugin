from typing import Any, Mapping, Optional, Dict
from werkzeug import Request, Response
from dify_plugin import Endpoint
from dify_plugin.invocations.file import UploadFileResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage, ImageSendMessage
import traceback
import hmac
import hashlib
import base64
import logging
import requests
import os
import re

logger = logging.getLogger(__name__)


class LineEndpoint(Endpoint):
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

                    # 檢查用戶訊息中的命令
                    if user_message.lower() == '/clearconversationhistory':
                        # 清除用戶對話歷史
                        self.session.storage.delete(key_to_check)

                        # 發送固定回覆
                        line_bot_api.reply_message(
                            event.reply_token,
                            TextSendMessage(
                                text="SYSTEM: Session history in Dify cleared.")
                        )

                        # 不進行處理直接返回
                        return Response(
                            status=200,
                            response="ok",
                            content_type="text/plain",
                        )

                response = self.session.app.chat.invoke(**invoke_params)
                answer = response.get("answer")
                conversation_id = response.get("conversation_id")
                # logger.debug("conversation_id:"+conversation_id)
                if conversation_id:
                    self.session.storage.set(
                        key_to_check, conversation_id.encode('utf-8'))
                # 檢查回答中的 markdown 圖片 URL
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
                # logger.debug(err)
                return Response(
                    status=500,
                    response=err,
                    content_type="text/plain",
                )

        @handler.add(MessageEvent, message=ImageMessage)
        def handle_image(event):
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
                return Response(
                    status=200,
                    response="ok",
                    content_type="text/plain",
                )
            try:
                content = line_bot_api.get_message_content(message_id)
                raw_bytes = content.content
                logger.debug(f"handle_image: retrieved {len(raw_bytes)} bytes")
                # 上傳文件到 Dify 並準備參數
                # 初始化 FileUploader 並通過 session 上傳
                uploader = FileUploader(
                    session=self.session, dify_api_key=dify_api_key)
                upload_resp = uploader.upload_file_via_api(
                    f"{message_id}.jpg", raw_bytes, "image/jpeg"
                )
                if not upload_resp:
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text="There is no image attached")
                    )
                    return Response(
                        status=200,
                        response="ok",
                        content_type="text/plain",
                    )
                file_param = upload_resp

                file_param["upload_file_id"] = file_param["id"]
                file_param["type"] = "image"
                file_param["transfer_method"] = "local_file"
                logger.debug(f"file_param: {file_param}")
                # 準備 Dify 輸入用於文件附件
                dify_inputs = {
                    img_variable_name: [file_param],
                }
                logger.debug(f"dify_inputs: {dify_inputs}")
            except Exception as e:
                logger.error(f"Error fetching image content: {e}")
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
            try:
                conversation_id = self.session.storage.get(key_to_check)
            except Exception:
                pass
            # 收集識別資訊
            identify_inputs = {
                "user_id": user_id,
                "group_id": group_id,
                "room_id": room_id,
            }
            # 合併圖片參數與識別資訊
            merged_inputs = {**dify_inputs, **identify_inputs}
            invoke_params = {
                "app_id": settings["app"]["app_id"],
                "query": img_prompt,
                "inputs": merged_inputs,
                "response_mode": "blocking",
            }
            if conversation_id is not None:
                invoke_params["conversation_id"] = conversation_id.decode(
                    'utf-8')
            response = self.session.app.chat.invoke(**invoke_params)
            logger.debug(f"handle_image: Dify invoke response: {response}")
            answer = response.get("answer")
            conversation_id = response.get("conversation_id")
            if conversation_id:
                self.session.storage.set(
                    key_to_check, conversation_id.encode('utf-8'))
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=answer)
            )
            return Response(
                status=200,
                response="ok",
                content_type="text/plain",
            )
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


class FileUploader:
    """
    A utility class to handle file uploads to Dify API
    """

    def __init__(
        self, session=None, dify_base_url="https://api.dify.ai/v1", dify_api_key=None
    ):
        """
        Initialize the FileUploader

        Args:
            session: The Dify plugin session object (if available)
            dify_base_url: The base URL for Dify API (if session not available)
            dify_api_key: The API key for Dify API (if session not available)
        """
        self.session = session
        self.dify_base_url = dify_base_url
        self.dify_api_key = dify_api_key

    def upload_file_via_session(
        self, filename: str, content: bytes, mimetype: str
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a file using the Dify plugin session

        Args:
            filename: The name of the file
            content: The content of the file as bytes
            mimetype: The MIME type of the file

        Returns:
            Dictionary with file information or None if upload fails
        """
        try:
            logger.debug(
                f"Uploading file via session: {filename}, mimetype: {mimetype}, content size: {len(content)} bytes"
            )

            # Use a simple test file first to verify the API is working
            test_result = self.session.file.upload(
                filename="test.txt", content=b"test content", mimetype="text/plain"
            )
            logger.debug(f"Test upload result: {test_result}")

            # Now try the actual file
            storage_file = self.session.file.upload(
                filename=filename, content=content, mimetype=mimetype
            )

            logger.debug(f"Uploaded file to Dify storage: {storage_file}")

            if storage_file:
                # Convert to app parameter format
                return storage_file.to_app_parameter()
            return None
        except Exception as e:
            logger.debug(f"Error uploading file via session: {e}")
            traceback.print_exc()
            return None

    def upload_file_via_api(
        self, filename: str, content: bytes, mimetype: str
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a file using direct API calls to Dify

        Args:
            filename: The name of the file
            content: The content of the file as bytes
            mimetype: The MIME type of the file

        Returns:
            Dictionary with file information or None if upload fails
        """
        if not self.dify_base_url or not self.dify_api_key:
            logger.debug(
                "Error: dify_base_url and dify_api_key must be provided for direct API upload"
            )
            return None

        try:
            logger.debug(
                f"Uploading file via API: {filename}, mimetype: {mimetype}, content size: {len(content)} bytes"
            )

            # Prepare the file upload endpoint
            upload_url = f"{self.dify_base_url}/files/upload"
            headers = {"Authorization": f"Bearer {self.dify_api_key}"}

            # Create a temporary file to upload
            temp_file_path = f"/tmp/{filename}"
            with open(temp_file_path, "wb") as f:
                f.write(content)

            # Upload the file
            files = {"file": (filename, open(temp_file_path, "rb"), mimetype)}

            response = requests.post(upload_url, headers=headers, files=files)

            # Clean up the temporary file
            os.remove(temp_file_path)

            if response.status_code == 201 or response.status_code == 200:
                result = response.json()
                logger.debug(f"File upload API response: {result}")

                # Format the response to match Dify plugin format
                if "id" in result:
                    return {
                        "id": result["id"],
                        "name": result.get("name", filename),
                        "size": result.get("size", len(content)),
                        "extension": result.get("extension", ""),
                        "mime_type": result.get("mime_type", mimetype),
                        "type": UploadFileResponse.Type.from_mime_type(mimetype).value,
                        "url": result.get("url", ""),
                    }
            else:
                logger.error(
                    f"File upload API error: {response.status_code}, {response.text}")

            return None
        except Exception as e:
            logger.error(f"Error uploading file via API: {e}")
            logger.error(traceback.format_exc())
            return None
