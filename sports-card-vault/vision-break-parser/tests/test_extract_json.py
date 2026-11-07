# -*- coding: utf-8 -*-
"""extract_json 回归测试。

大模型返回 JSON 的方式极不稳定：有的直接给纯 JSON，有的包 ```json 围栏，
有的在前后加一段"好的，以下是结果"之类的话。这一层清洗一旦失手，
整条解析链路就断了，而且错误信息会很难排查。
"""
import pytest

from parse_card import extract_json


@pytest.mark.parametrize(
    "raw",
    [
        '{"player_en": "Ryan Dunn", "numbered": "99/99"}',              # 纯 JSON
        '```json\n{"player_en": "Ryan Dunn", "numbered": "99/99"}\n```',  # json 围栏
        '```\n{"player_en": "Ryan Dunn", "numbered": "99/99"}\n```',      # 无语言标注围栏
        '好的，以下是解析结果：\n{"player_en": "Ryan Dunn", "numbered": "99/99"}\n希望有帮助',  # 夹带自然语言
    ],
)
def test_extract_json_accepts_common_model_output_shapes(raw):
    assert extract_json(raw) == {"player_en": "Ryan Dunn", "numbered": "99/99"}


def test_extract_json_preserves_nested_structure():
    raw = '```json\n{"break_info": {"year": "2025-26"}, "card_info": {"team_en": "Phoenix Suns"}}\n```'
    got = extract_json(raw)
    assert got["break_info"]["year"] == "2025-26"
    assert got["card_info"]["team_en"] == "Phoenix Suns"


@pytest.mark.parametrize("raw", ["", "   ", "\n\n"])
def test_extract_json_rejects_empty(raw):
    with pytest.raises(ValueError, match="为空"):
        extract_json(raw)


@pytest.mark.parametrize("raw", ["完全不是 JSON", "{坏的 JSON: 1}", "```json\nnot json\n```"])
def test_extract_json_raises_on_unparseable(raw):
    with pytest.raises(ValueError, match="JSON 解析失败"):
        extract_json(raw)
