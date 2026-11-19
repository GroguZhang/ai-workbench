# -*- coding: utf-8 -*-
"""Checklist 真值锚定 v3.5 核心测试套件。

覆盖：
1. Unicode NFKD 变音符折叠与无符号归一化（如 Luka Dončić -> lukadoncic）
2. 球员中英文别名与 SQL 预过滤词提取
3. 档位体系集合 TIER_KEYS（包含 base 与 parallel_unnumbered）
4. 序列编数标记提取 _serial_token（如 "25/99" -> "/99"）
5. 标准真值查询词构造 canonical_query
6. 无编折射平行增广 _augment_with_parallel（关键防塌缩：去除 Base、增广银折等中文词）
"""
import os
import sys
import unittest
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from app import checklist_anchor_v35 as v35


class TestChecklistAnchorV35(unittest.TestCase):

    def test_norm_diacritics_and_symbols(self):
        """测试 Unicode 变音符与特殊符号归一化（去除空格与标点）。"""
        self.assertEqual(v35._norm("Luka Dončić"), "lukadoncic")
        self.assertEqual(v35._norm("Nikola Jokić"), "nikolajokic")
        self.assertEqual(v35._norm("Kylian Mbappé"), "kylianmbappe")
        self.assertEqual(v35._norm("2024-25 Topps Chrome (Update)"), "202425toppschromeupdate")
        self.assertEqual(v35._norm("No. 125/199"), "no125199")

    def test_player_aliases_extraction(self):
        """测试中文、括号英文、姓氏别名提取。"""
        aliases = v35._player_aliases("卢卡·东契奇 (Luka Dončić)")
        self.assertIn("lukadoncic", aliases)
        self.assertIn("doncic", aliases)
        self.assertTrue(any("东契奇" in a or "卢卡" in a for a in aliases))

    def test_player_sql_terms(self):
        """测试 SQL 预过滤词生成。"""
        terms = v35._player_sql_terms("勒布朗·詹姆斯 (LeBron James)")
        self.assertTrue(len(terms) >= 1)
        for t in terms:
            self.assertNotIn("(", t)
            self.assertNotIn(")", t)

    def test_tier_keys_coverage(self):
        """测试 TIER_KEYS 完整性，确保新引入的 base 与 parallel_unnumbered 在册。"""
        self.assertIsInstance(v35.TIER_KEYS, set)
        self.assertIn("base", v35.TIER_KEYS)
        self.assertIn("parallel_unnumbered", v35.TIER_KEYS)
        self.assertIn("p_1_10", v35.TIER_KEYS)
        self.assertIn("s_1_10", v35.TIER_KEYS)
        self.assertIn("case_hit", v35.TIER_KEYS)

    def test_serial_token(self):
        """测试序列编数提取。"""
        self.assertEqual(v35._serial_token("25/99"), "/99")
        self.assertEqual(v35._serial_token("/10"), "/10")
        self.assertEqual(v35._serial_token(""), "")

    def test_canonical_query_generation(self):
        """测试标准真值查询词构造。"""
        row = {
            "year": "2023-24",
            "series": "Panini Prizm",
            "player_cn": "维克托·文班亚马",
            "player_en": "Victor Wembanyama",
            "card_number": "136",
        }
        q_cn = v35.canonical_query(row, lang="cn")
        self.assertEqual(q_cn, "2023-24 Panini Prizm 维克托·文班亚马 136")

        q_en = v35.canonical_query(row, lang="en")
        self.assertEqual(q_en, "2023-24 Panini Prizm Victor Wembanyama 136")

    def test_augment_with_parallel(self):
        """测试无编平行防估值塌缩增广（消除 Base 并注入中文折射词）。"""
        raw_query = "2025 Topps Chrome Cooper Flagg Base Refractor 251"
        augmented = v35._augment_with_parallel(raw_query, "Refractor", "parallel_unnumbered")
        self.assertNotIn(" Base ", augmented)
        self.assertIn("银折", augmented)


if __name__ == "__main__":
    unittest.main()
