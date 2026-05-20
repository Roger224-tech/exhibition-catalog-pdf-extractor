"""
Step 3: 字段抽取验证
===================
从版面分析后的文本块中抽取汽配相关字段，并对每个字段计算置信度。

核心策略 (混合):
  OE号/产品编号:  正则 (优先) + 位置规则
  品牌/制造商:     品牌词典 (优先) + NER
  车型适配:        正则 (年款模式、适用关键词)
  规格参数:        正则 (尺寸/材质/重量等模式)
  产品名称:        NER + 位置规则 (通常为卡片中字号最大的文本)
  价格:            正则 (货币符号 + 数字)
  原厂参考号:      正则 (OEM/REF等关键词)
  每包数量:        正则 (数字 + 单位)

用法:
    python step3_field_extraction.py ../step2_layout/sample_a_layout.json
    python step3_field_extraction.py ../step1_parsing/sample_a_parsed.json  # 跳过版面分析，直接用文本块
"""

import json
import os
import re
import sys
import time
import argparse
from pathlib import Path
from collections import Counter

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


# ═════════════════════════════════════════════════════
#  字段抽取规则库
# ═════════════════════════════════════════════════════

# ── OE号 / 产品编号 ──
# 无效OE号的过滤列表（常见误匹配文本）
OE_FALSE_POSITIVE_PATTERNS = [
    r'^\d{4}\s*[-–—]\s*\d{2,4}$',  # 纯年份范围: 2019-2020
    r'^OEM?\s',                      # 以OEM开头
    r'^PAGE\s*\d',                   # PAGE引用
    r'^\d{1,2}/\d{1,2}$',           # 分数
]

OE_PATTERNS = [
    # 供应商产品编号: SM-BMW-001, SM-BMW-037 (字母-字母-数字)
    (r'\b(SM-[A-Z]{3,4}\s*[-–—]\s*\d{3,4})\b', 0.90),
    # 供应商产品编号: PZ04C-001, PZ10C-026L/R (PZ+数字+字母-数字+可选L/R后缀)
    (r'\b(PZ\d{2,4}[A-Z]?\s*[-–—]\s*\d{3,4}(?:L/?R)?)\b', 0.90),
    # /XX-XXXX-XXX/ 斜杠包围格式: /BF-Q50-007/, /BF-HDCV-009/
    (r'/([A-Z]{2,4}\s*[-–—]\s*[A-Z0-9]{2,6}\s*[-–—]\s*\d{2,4})/', 0.92),
    # /XX-XXXX-XXX 半包围格式: /BF-HDCV-039 (右侧无斜杠)
    (r'/([A-Z]{2,4}\s*[-–—]\s*[A-Z0-9]{2,6}\s*[-–—]\s*\d{2,4})\b', 0.88),
    # AA-NNNN-NNN 格式 (汽配改造件常见不带斜杠): BF-Q50-007, BF-HDCV-009
    (r'\b([A-Z]{2,4})\s*[-–—]\s*([A-Z]{0,2}\d{1,4}[A-Z]*)\s*[-–—]\s*(\d{2,4})\b', 0.88),
    # 字母前缀 + 数字: BMW-345267, BOSCH 0986475 (长数字)
    (r'\b(?<![¥$€£])([A-Z]{1,5})[\s\-–—]{0,2}(\d{6,12})\b', 0.85),
    # 原厂OE号: 1T0807221, 2K5853677 (VW/Audi风格: 数字+字母+长数字)
    (r'\b([\d][A-Z0-9]{2}\s*\d{3}\s*\d{3,4}[A-Z]*)\b', 0.82),
    # OE号关键词引导: OE No.: L321-9876
    (r'(?:OE|OEM|O\.?E\.?)\s*(?:No|Number|号|编号)?[\.:\s]*([A-Z0-9][\w\-–—]{4,20})', 0.90),
    # 参考号关键词: Ref. 8K0941597
    (r'(?:Ref|参考|原厂)[\.:\s]*([A-Z0-9][\w\-–—]{4,20})', 0.85),
    # 数字-数字格式（置信度降低，容易误匹配年份）
    (r'\b(\d{5,8})\s*[-–—]\s*(\d{2,8})\b', 0.70),
    # 纯数字编号 (8-11位) - 最低优先级
    (r'\b(\d{8,11})\b', 0.55),
]

# ── 品牌/制造商 (词典) ──
BRAND_DICT_CN = {
    "博世": "Bosch", "采埃孚": "ZF", "大陆": "Continental", "电装": "Denso",
    "法雷奥": "Valeo", "海拉": "Hella", "马勒": "Mahle", "舍弗勒": "Schaeffler",
    "天合": "TRW", "德尔福": "Delphi", "曼牌": "MANN", "菲罗多": "Ferodo",
    "布雷博": "Brembo", "盖茨": "Gates", "日立": "Hitachi", "三菱": "Mitsubishi",
    "NTN": "NTN", "NSK": "NSK", "SKF": "SKF", "INA": "INA", "FAG": "FAG",
    "KYB": "KYB", "萨克斯": "SACHS", "卢卡斯": "Lucas", "辉门": "Federal-Mogul",
    "泰明顿": "Textar", "优锐": "TRW",
}

BRAND_DICT_EN = {
    "bosch": "Bosch", "zf": "ZF", "continental": "Continental", "denso": "Denso",
    "valeo": "Valeo", "hella": "Hella", "mahle": "Mahle", "schaeffler": "Schaeffler",
    "trw": "TRW", "delphi": "Delphi", "mann": "MANN", "ferodo": "Ferodo",
    "brembo": "Brembo", "gates": "Gates", "hitachi": "Hitachi",
    "ntn": "NTN", "nsk": "NSK", "skf": "SKF", "ina": "INA", "fag": "FAG",
    "kyb": "KYB", "sachs": "SACHS", "lucas": "Lucas",
    "federal-mogul": "Federal-Mogul", "textar": "Textar",
    "ngk": "NGK", "beru": "Beru", "vdo": "VDO", "lemforder": "Lemforder",
    "bilstein": "Bilstein", "eibach": "Eibach", "monroe": "Monroe",
    "dayco": "Dayco", "contitech": "ContiTech", "ina": "INA",
    # 汽配外贸/改造件品牌
    "aspp": "ASPP", "akm": "AKM", "xspeed": "XSpeed", "modesta": "Modesta",
    "cke": "CKE", "jp": "JP", "tyc": "TYC", "depo": "DEPO",
    "varis": "Varis", "ings": "INGS", "chargespeed": "ChargeSpeed",
    "mugen": "Mugen", "spoon": "Spoon", "hks": "HKS", "greddy": "Greddy",
    "apexi": "APEXi", "blitz": "Blitz", "tein": "TEIN", "cusc": "CUSCO",
    "tomei": "TOMEI", "sard": "SARD", "trust": "TRUST",
    "work": "WORK", "rays": "RAYS", "bbs": "BBS", "enkei": "ENKEI",
    "yokohama": "Yokohama", "toyo": "TOYO", "nitto": "NITTO",
    "akebono": "Akebono", "project-mu": "Project Mu", "endless": "ENDLESS",
    "dixcel": "DIXCEL", "oz": "OZ", "adv": "ADV", "vossen": "Vossen",
    "vorsteiner": "Vorsteiner", "mansory": "Mansory", "wald": "Wald",
    "brabus": "Brabus", "amg": "AMG", "ac-schnitzer": "AC Schnitzer",
    "alpina": "Alpina", "hamann": "Hamann", "techart": "Techart",
    "ruf": "RUF", "gemballa": "Gemballa",
}

# ── 车型适配 ──
CAR_MODEL_PATTERNS = [
    # 品牌 + 车系 + 年份
    (r'(宝马|BMW|奔驰|Benz|Mercedes|奥迪|Audi|大众|VW|Volkswagen|丰田|Toyota|本田|Honda|日产|Nissan|福特|Ford|现代|Hyundai|起亚|Kia)\s*([\w\d\-]+)?\s*(E\d{2,3}|F\d{2}|G\d{2}|W\d{3}|C\d{1,2})?\s*(\d{4}\s*[-~–]\s*\d{4})?', 0.82),
    # 通用车型关键词
    (r'(适用于?|适配|适合|匹配|Fit\s*(for)?|Compatible\s*with|For)\s*[：:]*\s*(.+)', 0.78),
    # 年份范围
    (r'(\d{4})\s*[-~–]\s*(\d{4})', 0.70),
]

# ── 规格参数 ──
SPEC_PATTERNS = [
    # 尺寸
    (r'(尺寸|Size|长度|宽度|高度|直径|Length|Width|Height|Diameter)[：:]*\s*(\d+\.?\d*\s*(mm|cm|m|inch|in|毫米|厘米))', 0.85, "尺寸"),
    # 重量
    (r'(重量|Weight|净重|毛重)[：:]*\s*(\d+\.?\d*\s*(kg|g|KG|G|lb|LBS|千克|克))', 0.85, "重量"),
    # 材质
    (r'(材质|Material|材料)[：:]*\s*([\u4e00-\u9fff\w]+)', 0.75, "材质"),
    # 电压
    (r'(电压|Voltage|V)[：:]*\s*(\d+\.?\d*\s*(V|v|伏))', 0.85, "电压"),
    # 功率
    (r'(功率|Power|W)[：:]*\s*(\d+\.?\d*\s*(W|w|KW|kw|瓦|千瓦))', 0.85, "功率"),
    # 通用规格
    (r'(规格|Spec|Specification|Parameters)[：:]*\s*(.+)', 0.65, "其他"),
]

# ── 价格 ──
PRICE_PATTERNS = [
    # 美元
    (r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)', 0.90, "USD"),
    # 欧元
    (r'€\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)', 0.90, "EUR"),
    # 人民币
    (r'¥\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)', 0.90, "CNY"),
    (r'(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*(元|RMB|CNY)', 0.85, "CNY"),
    # 关键词引导
    (r'(价格|Price|单价|Unit\s*Price)[：:]*\s*[\$€¥]?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)', 0.88, ""),
]

# ── 原厂参考号 (交叉引用) ──
OEM_REF_PATTERNS = [
    (r'(OEM|REF|Cross\s*Ref|原厂编号|参考号|互换号)[\.:\s]*([A-Z0-9][\w\-–—]{4,20})', 0.82),
    (r'(O\.?E\.?M\.?|Ref\.?)\s*[：:]*\s*([\w\-–—]{5,20})', 0.78),
]

# ── 每包数量 ──
PACK_QTY_PATTERNS = [
    (r'(\d+)\s*(个|只|件|套|PCS|pcs|pieces?|Sets?|套/盒|个/箱)', 0.85),
    (r'(包装|Pack|QTY|数量)[：:]*\s*(\d+)', 0.82),
]

# ── 产品名称 (用位置规则 + 常见产品词识别) ──
PRODUCT_NAME_INDICATORS = [
    "刹车片", "刹车盘", "刹车蹄", "制动片", "制动盘", "制动鼓",
    "减震器", "避震", "弹簧", "稳定杆", "控制臂", "摆臂",
    "保险杠", "大灯", "尾灯", "雾灯", "后视镜", "中网", "格栅",
    "机滤", "空滤", "空调滤", "燃油滤", "机油滤清器",
    "火花塞", "点火线圈", "氧传感器", "节气门",
    "水泵", "油泵", "发电机", "起动机", "压缩机",
    "散热器", "冷凝器", "蒸发器", "暖风水箱",
    "离合器", "分离轴承", "压盘", "离合片",
    "轮毂", "轴承", "油封", "密封圈",
    "皮带", "正时皮带", "张紧轮", "惰轮",
    "刹车油", "机油", "变速箱油", "防冻液", "冷媒",
]


# ═════════════════════════════════════════════════════
#  字段提取引擎
# ═════════════════════════════════════════════════════

class FieldExtractor:
    """汽配字段提取器"""

    def __init__(self):
        self.nlp_zh = None
        self.nlp_en = None
        self._init_nlp()

    def _init_nlp(self):
        """延迟加载NLP模型（首次使用时）"""
        pass  # 延迟到 extract 时加载，避免启动慢

    def extract_from_page(self, page_data: dict, page_num: int) -> list:
        """
        从单页数据中抽取所有产品字段

        输入: page_data = {
            "text_blocks": [{"text": ..., "bbox": [...], ...}, ...],
            "images": [...],
            "card_regions": [[x0,y0,x1,y1], ...]  # 可选
        }
        返回: products = [{字段...}, ...]
        """
        text_blocks = page_data.get("text_blocks", [])
        card_regions = page_data.get("card_regions", [])

        # 如果没有卡片区域，尝试基于OE号自动分割
        if not card_regions:
            card_regions = self._segment_by_oe_numbers(text_blocks, page_data)

        # 如果分割后仍然为空，整页当做一个产品
        if not card_regions:
            ps = page_data.get("page_size", [0, 0, 600, 800])
            default_bbox = [0, 0, ps[2] if len(ps) > 2 else 600, ps[3] if len(ps) > 3 else 800]
            card_regions = [default_bbox]

        products = []
        for card_idx, card_bbox in enumerate(card_regions):
            # 筛选属于该卡片的文本块
            card_blocks = self._filter_blocks_in_region(text_blocks, card_bbox)

            if not card_blocks:
                continue

            full_text = " ".join(b["text"] for b in card_blocks)

            product = {
                "page": page_num,
                "card_index": card_idx,
                "card_bbox": card_bbox,
                "raw_text": full_text[:500],
            }

            # 执行所有字段抽取
            product["oe_number"] = self._extract_oe_number(full_text)
            product["brand"] = self._extract_brand(full_text, card_blocks)
            product["vehicle_fitment"] = self._extract_vehicle(full_text)
            product["product_name"] = self._extract_product_name(full_text, card_blocks)
            product["specs"] = self._extract_specs(full_text)
            product["price"] = self._extract_price(full_text)
            product["oem_ref"] = self._extract_oem_ref(full_text)
            product["pack_qty"] = self._extract_pack_qty(full_text)
            product["description"] = self._extract_description(full_text, product)

            # 计算综合置信度
            confidences = [
                v.get("confidence", 0) if isinstance(v, dict) else 0
                for v in product.values()
                if isinstance(v, dict) and v.get("value")
            ]
            product["confidence_avg"] = round(sum(confidences) / max(len(confidences), 1), 2)

            products.append(product)

        return products

    def _segment_by_oe_numbers(self, text_blocks: list, page_data: dict) -> list:
        """
        基于OE号位置将页面自动分割为多个产品区域。

        策略: 找到所有OE号所在的文本块，以它们为锚点，
        将页面按y坐标拆分为多个产品区域。
        """
        if len(text_blocks) < 3:
            return []

        # 找到包含OE号的文本块索引
        oe_block_indices = []
        for i, blk in enumerate(text_blocks):
            text = blk.get("text", "")
            for pattern, _ in OE_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    oe_block_indices.append(i)
                    break

        # 如果找到多个OE号，以它们为锚点分割
        if len(oe_block_indices) >= 2:
            segments = []
            ps = page_data.get("page_size", [0, 0, 600, 800])
            page_w = ps[2] if len(ps) > 2 else 600
            page_h = ps[3] if len(ps) > 3 else 800

            used_blocks = set()
            for j, oe_idx in enumerate(oe_block_indices):
                if oe_idx in used_blocks:
                    continue

                # 确定该产品的文本块范围
                oe_block = text_blocks[oe_idx]
                oe_y_center = (oe_block["bbox"][1] + oe_block["bbox"][3]) / 2

                if j + 1 < len(oe_block_indices):
                    next_oe = text_blocks[oe_block_indices[j + 1]]
                    next_y = (next_oe["bbox"][1] + next_oe["bbox"][3]) / 2
                    split_y = (oe_y_center + next_y) / 2
                else:
                    split_y = page_h

                if j == 0:
                    prev_y = 0
                else:
                    prev_oe = text_blocks[oe_block_indices[j - 1]]
                    prev_y = (prev_oe["bbox"][1] + prev_oe["bbox"][3]) / 2
                    prev_y = (prev_y + oe_y_center) / 2

                # 收集该区域的文本块
                region_blocks = []
                region_x0, region_y0, region_x1, region_y1 = float("inf"), prev_y, 0, split_y
                for bi, blk in enumerate(text_blocks):
                    by = (blk["bbox"][1] + blk["bbox"][3]) / 2
                    if prev_y <= by <= split_y:
                        region_blocks.append(blk)
                        used_blocks.add(bi)
                        if blk["bbox"][0] < region_x0:
                            region_x0 = blk["bbox"][0]
                        if blk["bbox"][2] > region_x1:
                            region_x1 = blk["bbox"][2]

                if region_x0 == float("inf"):
                    region_x0 = 0
                if region_x1 == 0:
                    region_x1 = page_w

                if region_blocks:
                    segments.append([region_x0, region_y0, region_x1, region_y1])

            if len(segments) >= 2:
                return segments

        return []

    def _filter_blocks_in_region(self, blocks: list, region_bbox: list) -> list:
        """筛选位于指定区域内的文本块"""
        if not region_bbox or len(region_bbox) < 4:
            return blocks
        rx0, ry0, rx1, ry1 = region_bbox
        filtered = []
        for blk in blocks:
            bbox = blk.get("bbox", [])
            if not bbox or len(bbox) < 4:
                filtered.append(blk)
                continue
            # 块中心在区域内即算
            cx, cy = bbox_center(bbox)
            if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
                filtered.append(blk)
        return filtered if filtered else blocks  # 如果全部过滤掉了，返回原始

    # ── 各字段抽取方法 ──

    def _extract_oe_number(self, text: str) -> dict:
        """抽取OE号/产品编号"""
        candidates = []
        for pattern, base_conf in OE_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value = match.group(0) if match.lastindex is None else match.group()
                value = value.strip().strip('/').strip()
                # 过滤太短的匹配
                if len(value) < 4:
                    continue
                # 排除纯品牌名
                if value.upper() in BRAND_DICT_EN:
                    continue
                # 过滤已知的误匹配模式
                if self._is_oe_false_positive(value):
                    continue
                # 置信度微调：更长编号 → 更高置信度
                conf = min(0.95, base_conf + 0.01 * len(value))
                candidates.append({"value": value, "confidence": round(conf, 2), "method": "regex"})

        # 去重（按value），选最高置信度
        best = self._dedup_candidates(candidates)
        if best:
            return best[0]
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _is_oe_false_positive(self, value: str) -> bool:
        """检查是否为OE号的常见误匹配"""
        for fp_pattern in OE_FALSE_POSITIVE_PATTERNS:
            if re.search(fp_pattern, value, re.IGNORECASE):
                return True
        # 额外：纯年份范围如 2019-2020, 16-20
        if re.match(r'^\d{2,4}\s*[-–—]\s*\d{2,4}$', value):
            return True
        # 以OEM/OEM开头的
        if re.match(r'^OEM\b', value, re.IGNORECASE):
            return True
        return False

    def _extract_brand(self, text: str, blocks: list) -> dict:
        """抽取品牌（词典 + 位置规则）"""
        text_lower = text.lower()
        candidates = []

        # 词典匹配
        for brand_key, brand_name in {**BRAND_DICT_CN, **BRAND_DICT_EN}.items():
            brand_lower = brand_key.lower()
            # 短品牌名(<=3字符)必须用词边界匹配，避免"ina"匹配到"original"
            if len(brand_lower) <= 3:
                if not re.search(r'\b' + re.escape(brand_lower) + r'\b', text_lower):
                    continue
            else:
                if brand_lower not in text_lower:
                    continue

            # 额外校验：品牌必须是独立出现的（非其他单词的一部分）
            # 对于<=3字符的品牌，已通过word boundary保证
            # 对于4+字符的品牌，检查上下文
            if len(brand_lower) >= 4:
                # 如果匹配位置前后是字母，可能是误匹配
                idx = text_lower.index(brand_lower)
                before_ok = idx == 0 or not text_lower[idx-1].isalpha()
                after_ok = idx + len(brand_lower) >= len(text_lower) or not text_lower[idx + len(brand_lower)].isalpha()
                if not (before_ok or after_ok):
                    continue

            # 品牌词位于文本开头区域 → 更高置信度
            pos = text_lower.index(brand_lower)
            pos_ratio = pos / max(len(text), 1)
            conf = 0.90 - 0.15 * pos_ratio  # 越靠前置信度越高
            candidates.append({
                "value": brand_name,
                "confidence": round(conf, 2),
                "method": "dictionary",
            })

        # 字号最大块的文本可能是品牌名
        if blocks:
            max_font_block = max(blocks, key=lambda b: b.get("font_size_avg", 0))
            max_font_text = max_font_block.get("text", "").strip()
            if max_font_text and len(max_font_text) < 30:
                for brand_name in BRAND_DICT_EN.values():
                    if brand_name.lower() in max_font_text.lower():
                        candidates.append({
                            "value": brand_name,
                            "confidence": 0.88,
                            "method": "font_size_position",
                        })

        best = self._dedup_candidates(candidates)
        if best:
            return best[0]
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_vehicle(self, text: str) -> dict:
        """抽取车型适配信息"""
        candidates = []
        for pattern, base_conf in CAR_MODEL_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value = match.group(0) if match.lastindex is None else match.group()
                if len(value.strip()) < 3:
                    continue
                candidates.append({"value": value.strip(), "confidence": round(base_conf, 2), "method": "regex"})

        best = self._dedup_candidates(candidates)
        if best:
            return best[0]
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_product_name(self, text: str, blocks: list) -> dict:
        """抽取产品名称（字号 + 词典提示）"""
        candidates = []

        # 策略1: 产品关键词匹配
        for indicator in PRODUCT_NAME_INDICATORS:
            if indicator in text:
                # 找到包含该关键词的完整句子/短语
                idx = text.index(indicator)
                start = max(0, idx - 10)
                end = min(len(text), idx + len(indicator) + 20)
                phrase = text[start:end].strip()
                candidates.append({
                    "value": phrase[:80],
                    "confidence": 0.78,
                    "method": "keyword",
                })
                break  # 取第一个匹配

        # 策略2: 字号最大的文本行作为产品名
        if blocks:
            max_font_block = None
            max_font = 0
            for b in blocks:
                fs = b.get("font_size_avg", 0)
                if fs > max_font:
                    txt = b.get("text", "").strip()
                    # 跳过明显的非产品名
                    if len(txt) < 3 or len(txt) > 60:
                        continue
                    if re.match(r'^[\$\€\¥]', txt):
                        continue
                    if re.match(r'^\d{4,}', txt):
                        continue
                    # 跳过页码引用: PAGE 254, PAGE257
                    if re.match(r'^PAGE\s*\d', txt, re.IGNORECASE):
                        continue
                    # 跳过纯数字
                    if re.match(r'^\d+$', txt):
                        continue
                    # 跳过纯符号/装饰字符
                    if re.match(r'^[□��\s]+$', txt):
                        continue
                    max_font = fs
                    max_font_block = b

            if max_font_block:
                candidates.append({
                    "value": max_font_block["text"].strip(),
                    "confidence": 0.72,
                    "method": "font_size",
                })

        # 合并候选，优先关键词匹配
        kw_candidates = [c for c in candidates if c["method"] == "keyword"]
        if kw_candidates:
            return kw_candidates[0]
        font_candidates = [c for c in candidates if c["method"] == "font_size"]
        if font_candidates:
            return font_candidates[0]
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_specs(self, text: str) -> dict:
        """抽取规格参数 (返回多个规格的列表)"""
        specs = []
        for pattern, base_conf, spec_type in SPEC_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value = match.group(0).strip()
                if len(value) < 3:
                    continue
                specs.append({
                    "type": spec_type,
                    "value": value,
                    "confidence": round(base_conf, 2),
                    "method": "regex",
                })

        if specs:
            return {
                "value": "; ".join([s["value"] for s in specs]),
                "items": specs,
                "confidence": round(sum(s["confidence"] for s in specs) / len(specs), 2),
                "method": "regex",
            }
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_price(self, text: str) -> dict:
        """抽取价格"""
        for pattern, base_conf, currency in PRICE_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(0).strip()
                return {
                    "value": value,
                    "currency": currency,
                    "confidence": round(base_conf, 2),
                    "method": "regex",
                }
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_oem_ref(self, text: str) -> dict:
        """抽取原厂参考号"""
        for pattern, base_conf in OEM_REF_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(0).strip()
                return {"value": value, "confidence": round(base_conf, 2), "method": "regex"}
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_pack_qty(self, text: str) -> dict:
        """抽取每包数量"""
        for pattern, base_conf in PACK_QTY_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(0).strip()
                return {"value": value, "confidence": round(base_conf, 2), "method": "regex"}
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_description(self, text: str, product: dict) -> dict:
        """抽取简要描述（剩余未匹配的文本中的描述性内容）"""
        # 移除已抽取的字段值
        remaining = text
        for key in ["oe_number", "brand", "vehicle_fitment", "product_name", "price", "oem_ref"]:
            val = product.get(key, {})
            if isinstance(val, dict) and val.get("value"):
                remaining = remaining.replace(val["value"], "", 1)

        # 取剩余文本中长度适中的句子作为描述
        remaining = remaining.strip()
        if 10 < len(remaining) < 200:
            return {"value": remaining, "confidence": 0.55, "method": "residual"}
        elif len(remaining) >= 200:
            return {"value": remaining[:200] + "...", "confidence": 0.50, "method": "residual"}
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _dedup_candidates(self, candidates: list) -> list:
        """去重并按置信度排序"""
        seen = set()
        unique = []
        for c in sorted(candidates, key=lambda x: x["confidence"], reverse=True):
            key = c["value"].lower().strip()
            if key not in seen and len(key) >= 2:
                seen.add(key)
                unique.append(c)
        return unique


# ═════════════════════════════════════════════════════
#  辅助函数
# ═════════════════════════════════════════════════════

def bbox_center(bbox):
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


# ═════════════════════════════════════════════════════
#  报告生成
# ═════════════════════════════════════════════════════

def print_field_report(all_products: list, total_time: float):
    """打印字段抽取报告"""
    print("\n" + "=" * 60)
    print("  字段抽取报告")
    print("=" * 60)
    print(f"  总产品数:        {len(all_products)}")
    print(f"  处理耗时:        {total_time:.2f} 秒")

    # 统计各字段命中率
    field_stats = defaultdict(lambda: {"hit": 0, "avg_conf": 0.0})
    for prod in all_products:
        for field in ["oe_number", "brand", "vehicle_fitment", "product_name",
                       "specs", "price", "oem_ref", "pack_qty", "description"]:
            val = prod.get(field, {})
            if isinstance(val, dict) and val.get("value"):
                field_stats[field]["hit"] += 1
                field_stats[field]["avg_conf"] += val.get("confidence", 0)

    n = max(len(all_products), 1)
    print(f"\n  {'字段':<16} {'命中数':<8} {'命中率':<10} {'平均置信度':<12}")
    print("  " + "-" * 48)
    for field in field_stats:
        s = field_stats[field]
        hit_rate = s["hit"] / n * 100
        avg_c = s["avg_conf"] / max(s["hit"], 1) * 100
        print(f"  {field:<16} {s['hit']:<8} {hit_rate:<10.0f}% {avg_c:<12.0f}%")

    # 综合评估
    oe_hit = field_stats["oe_number"]["hit"] / n * 100
    brand_hit = field_stats["brand"]["hit"] / n * 100
    print(f"\n  ── 关键指标 ──")
    print(f"  OE号提取率:      {oe_hit:.0f}%  {'[OK]' if oe_hit >= 85 else '[FAIL] 目标>=85%'}")
    print(f"  品牌识别率:      {brand_hit:.0f}%  {'[OK]' if brand_hit >= 80 else '[FAIL] 目标>=80%'}")

    print("=" * 60 + "\n")


# ═════════════════════════════════════════════════════
#  主入口
# ═════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Step 3: 字段抽取验证")
    parser.add_argument("input_path", help="Step1 parsed JSON 或 Step2 layout JSON")
    parser.add_argument("-o", "--output", default="step3_fields", help="输出目录")
    args = parser.parse_args()

    input_path = Path(args.input_path)
    if not input_path.exists():
        print(f"错误: 文件不存在 {input_path}")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(args.output, exist_ok=True)

    extractor = FieldExtractor()
    all_products = []
    t0 = time.time()

    # 判断输入数据类型
    pages = data.get("pages", [])

    for page_data in pages:
        # 兼容 Step1 和 Step2 的数据格式
        page_num = page_data.get("page_num", page_data.get("page_num", 0))

        # Step1 格式: text_blocks 在页面顶层
        # Step2 格式: blocks 嵌套在 page 内
        normalized = {
            "text_blocks": page_data.get("text_blocks", page_data.get("blocks", [])),
            "images": page_data.get("images", []),
            "card_regions": page_data.get("card_regions", []),
            "page_size": page_data.get("page_size", [0, 0, 600, 800]),
        }

        if page_data.get("is_scanned") and not page_data.get("ocr_applied"):
            print(f"  跳过第 {page_num} 页 (扫描件，无OCR)")
            continue

        products = extractor.extract_from_page(normalized, page_num)
        all_products.extend(products)

    total_time = time.time() - t0

    print_field_report(all_products, total_time)

    # 保存结果
    prefix = Path(data.get("filename", input_path.stem)).stem
    json_out = os.path.join(args.output, f"{prefix}_fields.json")
    output_data = {
        "source_file": data.get("filename", str(input_path)),
        "total_products": len(all_products),
        "extraction_time_seconds": round(total_time, 2),
        "products": all_products,
    }
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"结果已保存: {json_out}")


if __name__ == "__main__":
    main()
