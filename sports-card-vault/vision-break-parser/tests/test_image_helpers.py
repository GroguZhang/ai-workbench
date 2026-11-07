# -*- coding: utf-8 -*-
"""图片辅助函数测试（MIME 推断与 Base64 编码）。"""
import base64

import pytest

from parse_card import SUPPORTED_EXT, encode_image_to_base64, mime_type_for


@pytest.mark.parametrize(
    "path,expected",
    [
        ("a.jpg", "image/jpeg"),
        ("a.JPEG", "image/jpeg"),   # 大小写不敏感
        ("a.png", "image/png"),
        ("a.webp", "image/webp"),
        ("a.gif", "image/gif"),
        ("a.bmp", "image/bmp"),
        ("a.unknown", "image/jpeg"),  # 未知扩展回落 jpeg
        ("noext", "image/jpeg"),
    ],
)
def test_mime_type_for(path, expected):
    assert mime_type_for(path) == expected


def test_encode_image_to_base64_roundtrips(tmp_path):
    payload = b"\x89PNG\r\n\x1a\n fake image bytes"
    p = tmp_path / "card.png"
    p.write_bytes(payload)

    encoded = encode_image_to_base64(str(p))
    assert base64.b64decode(encoded) == payload


def test_encode_image_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        encode_image_to_base64(str(tmp_path / "nope.png"))


def test_encode_image_unsupported_extension_raises(tmp_path):
    p = tmp_path / "card.txt"
    p.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="不支持的图片格式"):
        encode_image_to_base64(str(p))


def test_supported_ext_matches_mime_table():
    # 这两个表必须同步，否则会出现「能编码但 MIME 回落成 jpeg」的错配
    for ext in SUPPORTED_EXT:
        assert mime_type_for("x" + ext) != "", ext
