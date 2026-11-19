# -*- coding: utf-8 -*-
"""定价引擎 v1 核心测试套件。

覆盖：
1. 六维严格分桶键 bucket_key (year_set, player, card_number, parallel, denom, grade)
2. 编数档位分桶 _denom_bucket 与评级分桶 _grade_bucket
3. 成交时间分段衰减加权 age_weight (30天/90天/更早) 与加权中位数 weighted_median
4. 四分位距极值抗噪滤波 iqr_filter
5. 样本真实性抗噪锚定校验 anchor_check (卡号/编数/平行/特卡，纸卡噪音拦截)
6. 估值来源标签判定 source_label (实盘中位数 / 阶梯推演 / 人工锚定 / 样本不足)
"""
import os
import sys
import unittest
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from app import pricing_engine_v1 as pe


class TestPricingEngineV1(unittest.TestCase):

    def test_bucket_key_6d(self):
        """测试六维严格分桶元组。"""
        card = {
            "sport": "篮球",
            "player": "Luka Doncic (卢卡·东契奇)",
            "year_set": "2024-25 Topps Chrome",
            "card_number": "136",
            "parallel": "Gold Refractor",
            "serial": "10/50",
            "grader": "PSA",
            "grade_score": 10
        }
        bkey = pe.bucket_key(card)
        self.assertEqual(len(bkey), 6)
        self.assertEqual(bkey[0], "202425toppschrome")
        self.assertEqual(bkey[1], "lukadoncic")
        self.assertEqual(bkey[2], "136")
        self.assertEqual(bkey[3], "gold refractor")
        self.assertEqual(bkey[4], "35_50")
        self.assertEqual(bkey[5], "PSA10")

    def test_denom_and_grade_bucket(self):
        """测试编数分桶与评级分桶。"""
        self.assertEqual(pe._denom_bucket(5), "1_10")
        self.assertEqual(pe._denom_bucket(25), "15_25")
        self.assertEqual(pe._denom_bucket(99), "75_99")
        self.assertEqual(pe._denom_bucket(None), "base")

        # 未评级 raw（未设置 grader 或 grader 为空）
        self.assertEqual(pe._grade_bucket({}), "raw")
        self.assertEqual(pe._grade_bucket({"grader": ""}), "raw")
        # 评级分桶
        self.assertEqual(pe._grade_bucket({"grader": "PSA", "grade_score": 10}), "PSA10")
        self.assertEqual(pe._grade_bucket({"grader": "BGS", "grade_score": 9.5}), "BGS9.5")

    def test_age_weight(self):
        """测试成交时间加权。"""
        self.assertEqual(pe.age_weight(10), 1.0)
        self.assertEqual(pe.age_weight(45), 0.6)
        self.assertEqual(pe.age_weight(120), 0.3)

    def test_weighted_median(self):
        """测试加权中位数计算。"""
        # 对称样本
        pairs = [(100.0, 1.0), (200.0, 1.0), (300.0, 1.0)]
        self.assertEqual(pe.weighted_median(pairs), 200.0)

        # 偏向近期样本
        pairs_skewed = [(100.0, 0.1), (200.0, 0.2), (300.0, 0.9)]
        self.assertEqual(pe.weighted_median(pairs_skewed), 300.0)

        # 空列表返回 0.0
        self.assertEqual(pe.weighted_median([]), 0.0)

    def test_iqr_filter(self):
        """测试四分位距极值过滤。"""
        normal_prices = [100.0, 105.0, 110.0, 115.0, 120.0, 1000.0]
        filtered = pe.iqr_filter(normal_prices)
        self.assertNotIn(1000.0, filtered)
        self.assertIn(110.0, filtered)

    def test_anchor_check_rejects_mismatch(self):
        """测试样本抗噪锚定校验。"""
        card = {
            "card_number": "136",
            "serial": "10/25",
            "parallel": "Gold Refractor",
            "card_type": "Base"
        }
        # 命中卡号但全无折射/编数字样 -> 判定为纸卡噪音
        sale_paper_noise = {"title": "2024 Topps Chrome Luka Doncic #136 Base Card", "price": 10}
        ok, reason = pe.anchor_check(card, sale_paper_noise, set())
        self.assertFalse(ok)
        self.assertEqual(reason, "parallel_noise")

        # 缺少平行版本与编数与卡号
        sale_mismatch = {"title": "2024 Topps Chrome Other Player Card", "price": 100}
        ok, reason = pe.anchor_check(card, sale_mismatch, set())
        self.assertFalse(ok)
        self.assertEqual(reason, "no_parallel")

        # 正常命中卡号与编数
        sale_hit = {"title": "2024 Topps Chrome #136 Luka Doncic Gold Refractor /25", "price": 1500}
        ok, reason = pe.anchor_check(card, sale_hit, set())
        self.assertTrue(ok)
        self.assertIn("number", reason)
        self.assertIn("serial", reason)

    def test_source_label(self):
        """测试估值来源标签依据。"""
        self.assertEqual(pe.source_label("按同款实盘加权中位数 (5笔)"), "实盘中位数")
        self.assertEqual(pe.source_label("按 2024 Topps Chrome 阶梯推演"), "阶梯推演")
        self.assertEqual(pe.source_label("样本不足，保留人工锚定"), "人工锚定")
        self.assertEqual(pe.source_label("", status="insufficient"), "样本不足")
        self.assertEqual(pe.source_label("", status=""), "—")


if __name__ == "__main__":
    unittest.main()
