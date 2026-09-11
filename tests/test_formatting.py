"""
build_messages 的單元測試：把 Dify 回答轉成 LINE 訊息陣列。
涵蓋文字切段（5000 字）、圖片抽取、5 則上限、FlexMessage 開關與失敗降級。
"""
from unittest.mock import patch

from core.formatting import build_messages, split_text


def test_plain_answer_becomes_single_text_message():
    msgs = build_messages("hello", use_flex=False)
    assert msgs == [{"type": "text", "text": "hello"}]


def test_long_answer_is_split_at_newline_under_5000_chars():
    answer = ("a" * 3000) + "\n" + ("b" * 3000)
    msgs = build_messages(answer, use_flex=False)
    assert len(msgs) == 2
    assert msgs[0]["text"] == "a" * 3000
    assert msgs[1]["text"] == "b" * 3000
    assert all(len(m["text"]) <= 5000 for m in msgs)


def test_split_text_hard_cuts_when_no_newline():
    chunks = split_text("x" * 12000, 5000)
    assert [len(c) for c in chunks] == [5000, 5000, 2000]


def test_markdown_images_become_image_messages_and_are_removed_from_text():
    answer = "Here you go ![pic](https://img.example/a.png)"
    msgs = build_messages(answer, use_flex=False)
    assert msgs[0] == {"type": "text", "text": "Here you go"}
    assert msgs[1] == {
        "type": "image",
        "originalContentUrl": "https://img.example/a.png",
        "previewImageUrl": "https://img.example/a.png",
    }


def test_messages_are_capped_at_five():
    answer = " ".join(f"![i]({'https://x.example/%d.png' % i})" for i in range(8))
    msgs = build_messages(answer, use_flex=False)
    assert len(msgs) == 5


def test_flex_is_used_only_for_markdown_answers_when_enabled():
    plain = build_messages("just text", use_flex=True)
    assert plain[0]["type"] == "text"
    rich = build_messages("| a | b |\n|---|---|\n| 1 | 2 |", use_flex=True)
    assert rich[0]["type"] == "flex"
    assert rich[0]["contents"]["type"] == "bubble"


def test_flex_conversion_failure_falls_back_to_text():
    with patch("core.formatting.MdFlexFormatHelper.md_to_flex", side_effect=ValueError("boom")):
        msgs = build_messages("```code```", use_flex=True)
    assert msgs[0]["type"] == "text"
    assert "code" in msgs[0]["text"]


def test_empty_answer_yields_fallback_text():
    msgs = build_messages("", use_flex=False)
    assert len(msgs) == 1 and msgs[0]["type"] == "text" and msgs[0]["text"]
