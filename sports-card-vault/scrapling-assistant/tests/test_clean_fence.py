# -*- coding: utf-8 -*-
"""clean_fence 回归测试。

视觉模型对「只输出 JSON」的服从度并不稳定：有时老老实实给 JSON，
有时裹一层代码块。这一层清洗挂了，解析结果就会退化成 {"raw_output": ...}，
调用方拿不到任何字段但也不会报错——是最难发现的一类静默失败。
"""
import pytest

from app import clean_fence

FENCE = "```"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (FENCE + 'json\n{"a": 1}\n' + FENCE, '{"a": 1}'),
        (FENCE + '\n{"a": 1}\n' + FENCE, '{"a": 1}'),
        (FENCE + 'json\n{"a": 1}', '{"a": 1}'),        # 只有开头围栏
        ('{"a": 1}', '{"a": 1}'),                        # 本来就是纯 JSON
        ('  {"a": 1}  ', '{"a": 1}'),                    # 前后空白
        ('', ''),
        (None, ''),
    ],
)
def test_clean_fence(raw, expected):
    assert clean_fence(raw) == expected


def test_clean_fence_leaves_mid_text_fence_untouched():
    # 已知局限：围栏出现在文本中间时不处理。钉住现状，
    # 将来若要支持「解释 + 代码块」混合输出，这条测试会提示需要同步修改。
    raw = '前置说明 ' + FENCE + 'json\n{"a": 1}' + FENCE
    assert clean_fence(raw) == raw


def test_clean_fence_output_is_json_parsable():
    import json

    for raw in (
        FENCE + 'json\n{"k": "v"}\n' + FENCE,
        FENCE + '\n{"k": "v"}\n' + FENCE,
        '{"k": "v"}',
    ):
        assert json.loads(clean_fence(raw)) == {"k": "v"}
