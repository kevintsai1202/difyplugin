"""
把 Dify 的 Markdown 回答轉成 LINE 訊息陣列（展現層）。

規則：
- 先抽出 Markdown 圖片 ![alt](url) 轉成 image 訊息，其餘文字另外處理
- 啟用 FlexMessage 且文字含表格／連結／程式碼區塊時轉成 flex，失敗降級為純文字
- 純文字超過 5000 字在換行處切段
- 總數不超過 5 則
"""
import logging
import re
from typing import List

from markdown_it import MarkdownIt

from .constants import LINE_MAX_TEXT, LINE_MAX_MESSAGES

logger = logging.getLogger(__name__)

# Markdown 圖片語法
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")
# 判斷是否值得轉 FlexMessage：表格、連結、程式碼區塊
_RICH_RE = re.compile(r"\|.*\|.*\||\[.*\]\(.*\)|```", re.S)


def split_text(text: str, max_len: int = LINE_MAX_TEXT) -> List[str]:
    """把長文字切成多段，每段不超過 max_len，盡量在換行處切。"""
    chunks: List[str] = []
    while len(text) > max_len:
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text or not chunks:
        chunks.append(text)
    return chunks


def text_message(text: str) -> dict:
    return {"type": "text", "text": text}


def image_message(url: str) -> dict:
    return {"type": "image", "originalContentUrl": url, "previewImageUrl": url}


def build_messages(answer: str, use_flex: bool) -> List[dict]:
    """
    把 Dify 回答轉成可直接送給 LINE 的訊息陣列（最多 5 則）。

    Args:
        answer: Dify 的回答（Markdown）
        use_flex: 是否啟用 Markdown 轉 FlexMessage
    """
    answer = answer or ""
    image_urls = _IMAGE_RE.findall(answer)
    text = _IMAGE_RE.sub("", answer).strip()

    messages: List[dict] = []
    if text:
        flex = None
        if use_flex and _RICH_RE.search(text):
            try:
                flex = MdFlexFormatHelper().md_to_flex(text)
            except Exception as e:
                logger.warning("FlexMessage conversion failed, falling back to text: %s", e)
        if flex:
            messages.append({"type": "flex", "altText": text[:400], "contents": flex})
        else:
            messages.extend(text_message(chunk) for chunk in split_text(text))

    for url in image_urls:
        if len(messages) >= LINE_MAX_MESSAGES:
            break
        messages.append(image_message(url))

    if not messages:
        messages.append(text_message("(empty response)"))
    return messages[:LINE_MAX_MESSAGES]


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
