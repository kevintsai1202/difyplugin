from typing import Any, Mapping, Optional, Dict
from werkzeug import Request, Response
from dify_plugin import Endpoint
from dify_plugin.invocations.file import UploadFileResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage, ImageSendMessage, FlexSendMessage, BubbleContainer, BoxComponent, TextComponent
import traceback
import hmac
import hashlib
import base64
import logging
import requests
import os
import re
from markdown_it import MarkdownIt

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
                # md to flex
                if settings.get("mdtoflex") and (re.search(r'\|.*\|.*\|', answer) or re.search(r'\[.*\]\(.*\)', answer) or '```' in answer):
                    logger.debug(
                        f"Converting markdown to FlexMessage: {answer[:100]}...")
                    try:
                        format_helper = MdFlexFormatHelper()
                        flex_message = format_helper.md_to_flex(answer)
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
                    session=self.session, dify_api_key=dify_api_key, dify_base_url=settings.get('dify_api_url'))
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


class MdFlexFormatHelper:
    """
    Convert markdown text to a LINE Flex Message JSON structure
    """

    def __init__(self):
        self.md = MarkdownIt()
        logger.debug("MdFlexFormatHelper initialized")

    def md_to_flex(self, md_text: str) -> dict:
        """Convert markdown text to a LINE Flex Message JSON structure

        Args:
            md_text: Markdown text to convert

        Returns:
            A dictionary representing a LINE Flex Bubble container
        """
        logger.debug(f"Parsing markdown: {md_text[:100]}...")

        # Process tables first with a custom approach
        has_table = '|' in md_text and re.search(r'\|.*\|.*\|', md_text)
        table_contents = []

        if has_table:
            logger.debug("Table detected, processing table format")
            # Extract table rows
            table_rows = []
            current_table = []
            in_table = False

            for line in md_text.split('\n'):
                if '|' in line and not line.strip().startswith('```'):
                    if not in_table:
                        in_table = True
                    current_table.append(line)
                elif in_table:
                    # Table ended
                    if current_table:
                        table_rows.append(current_table)
                        current_table = []
                    in_table = False

            # Add any remaining table
            if current_table:
                table_rows.append(current_table)

            # Process each table
            for table in table_rows:
                if len(table) < 2:  # Need at least header and separator
                    continue

                # Process header row
                header_cells = [cell.strip()
                                for cell in table[0].split('|') if cell.strip()]

                # Check if second row is separator (---|---)
                is_separator = all(
                    '-' in cell for cell in table[1].split('|') if cell.strip())
                start_idx = 2 if is_separator else 1

                # Create table component with border
                table_component = {
                    "type": "box",
                    "layout": "vertical",
                    "margin": "md",
                    "spacing": "none",  # Remove spacing between rows for grid effect
                    "borderColor": "#CCCCCC",
                    "borderWidth": "1px",
                    "cornerRadius": "md",
                    "contents": []
                }

                # Add header row
                header_row = {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": [],
                    "backgroundColor": "#EEEEEE",
                    "paddingAll": "sm",  # Smaller padding
                    "borderColor": "#CCCCCC",
                    "borderWidth": "1px"
                }

                # Create header cells
                for cell in header_cells:
                    header_row["contents"].append({
                        "type": "box",
                        "layout": "vertical",
                        "contents": [{
                            "type": "text",
                            "text": cell,
                            "weight": "bold",
                            "size": "xs",  # Smaller text
                            "align": "start",  # Left align
                            "wrap": True
                        }],
                        "paddingAll": "sm",
                        "width": f"{100/len(header_cells)}%",
                        "borderColor": "#CCCCCC",
                        "borderWidth": "1px"
                    })

                table_component["contents"].append(header_row)

                # Add data rows
                for row_idx in range(start_idx, len(table)):
                    row = table[row_idx]
                    cells = [cell.strip()
                             for cell in row.split('|') if cell.strip()]

                    if not cells:
                        continue

                    data_row = {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [],
                        "paddingAll": "xs",  # Smaller padding
                        "backgroundColor": "#FFFFFF" if row_idx % 2 == 0 else "#F8F8F8",
                        "borderColor": "#CCCCCC",
                        "borderWidth": "1px"
                    }

                    # Create cells with same width as headers for alignment
                    for idx, cell in enumerate(cells):
                        if idx >= len(header_cells):
                            break  # Skip extra cells

                        # Process cell content for bold and other formatting
                        cell_text = cell
                        weight = "regular"

                        # Handle bold text with ** or __
                        if re.search(r'\*\*(.*?)\*\*', cell) or re.search(r'__(.*?)__', cell):
                            # Extract bold text
                            bold_matches = re.findall(
                                r'\*\*(.*?)\*\*|__(.*?)__', cell)
                            for match in bold_matches:
                                bold_text = match[0] if match[0] else match[1]
                                cell_text = cell_text.replace(
                                    f"**{bold_text}**", bold_text)
                                cell_text = cell_text.replace(
                                    f"__{bold_text}__", bold_text)
                            weight = "bold"

                        # Create cell with border
                        data_row["contents"].append({
                            "type": "box",
                            "layout": "vertical",
                            "contents": [{
                                "type": "text",
                                "text": cell_text,
                                "size": "xs",  # Smaller text
                                "align": "start",  # Left align
                                "wrap": True,
                                "weight": weight
                            }],
                            "paddingAll": "sm",
                            "width": f"{100/len(header_cells)}%",
                            "borderColor": "#CCCCCC",
                            "borderWidth": "1px"
                        })

                    # Add empty cells if needed to match header count
                    while len(data_row["contents"]) < len(header_cells):
                        data_row["contents"].append({
                            "type": "box",
                            "layout": "vertical",
                            "contents": [{
                                "type": "text",
                                "text": " ",
                                "size": "xs",
                                "align": "start"
                            }],
                            "paddingAll": "sm",
                            "width": f"{100/len(header_cells)}%",
                            "borderColor": "#CCCCCC",
                            "borderWidth": "1px"
                        })

                    table_component["contents"].append(data_row)

                table_contents.append(table_component)

        # Process the rest of the markdown
        non_table_content = md_text
        if has_table:
            # Remove table content from markdown text
            for line in md_text.split('\n'):
                if '|' in line and not line.strip().startswith('```'):
                    non_table_content = non_table_content.replace(line, '')

        # Extract images first
        image_components = []
        image_pattern = r'!\[(.*?)\]\((.*?)\)'

        # Find all image references and create image components
        for match in re.finditer(image_pattern, non_table_content):
            alt_text = match.group(1)
            image_url = match.group(2)
            logger.debug(f"Found image: {alt_text}, URL: {image_url}")

            # Create image component
            image_components.append({
                "type": "image",
                "url": image_url,
                "size": "full",
                "aspectMode": "fit",
                "aspectRatio": "1:1",
                "margin": "md"
            })

            # Remove the image markdown from the text to avoid duplication
            non_table_content = non_table_content.replace(
                f"![{alt_text}]({image_url})", "")

        # Create components for the flex message
        body_contents = []

        # Process headings and paragraphs
        for line in non_table_content.split('\n'):
            line = line.strip()
            if not line:
                continue

            # Handle headings
            heading_match = re.match(r'^(#+)\s+(.+)$', line)
            if heading_match:
                level = len(heading_match.group(1))
                text = heading_match.group(2)

                size = "xl" if level <= 2 else "lg" if level == 3 else "md"

                body_contents.append({
                    "type": "text",
                    "text": text,
                    "weight": "bold",
                    "size": size,
                    "margin": "md",
                    "wrap": True
                })
                continue

            # Skip empty lines after image removal
            if not line.strip():
                continue

            # Handle bold text in paragraphs
            if '**' in line or '__' in line:
                # We'll handle this as a special text with spans
                text_component = {
                    "type": "text",
                    "text": line,  # Fallback text
                    "wrap": True,
                    "size": "md"
                }

                # Try to create spans for bold text
                spans = []
                bold_pattern = r'\*\*(.*?)\*\*|__(.*?)__'
                last_end = 0
                has_bold = False

                for match in re.finditer(bold_pattern, line):
                    has_bold = True
                    bold_text = match.group(1) if match.group(
                        1) else match.group(2)
                    start = match.start()
                    end = match.end()

                    # Add non-bold text before this match
                    if start > last_end:
                        spans.append({
                            "type": "span",
                            "text": line[last_end:start],
                            "size": "md"
                        })

                    # Add bold text
                    spans.append({
                        "type": "span",
                        "text": bold_text,
                        "weight": "bold",
                        "size": "md"
                    })

                    last_end = end

                # Add any remaining text
                if last_end < len(line):
                    spans.append({
                        "type": "span",
                        "text": line[last_end:],
                        "size": "md"
                    })

                if has_bold and spans:
                    text_component["contents"] = spans

                body_contents.append(text_component)
            elif line and not line.startswith('|'):
                # Regular paragraph
                body_contents.append({
                    "type": "text",
                    "text": line,
                    "wrap": True,
                    "size": "md"
                })

        # Add image components
        body_contents.extend(image_components)

        # Add table contents after processing the rest
        body_contents.extend(table_contents)

        # Create the bubble container structure
        bubble = {
            "type": "bubble",
            "size": "giga",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": body_contents,
                "spacing": "md",
                "paddingAll": "xl"
            }
        }

        logger.debug(f"Created bubble with {len(body_contents)} components")
        return bubble
