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

        # ── 检测页面列结构 (双栏/单栏) ──
        columns = self._detect_columns(text_blocks, page_data)

        # ── 为每列提取顶部车型和品牌 ──
        column_vehicle = {}
        column_brand = {}
        for col_id, (col_x0, col_x1) in columns.items():
            ps = page_data.get("page_size", [600, 800])
            page_h = ps[-1] if len(ps) >= 2 else 800
            top_blocks = [b for b in text_blocks
                          if col_x0 <= bbox_center(b["bbox"])[0] <= col_x1
                          and b["bbox"][1] < page_h * 0.35]
            column_vehicle[col_id] = self._extract_top_vehicle(top_blocks)
            column_brand[col_id] = self._extract_top_brand(top_blocks)

        # ── 确定产品区域和对应的文本块 ──
        if card_regions:
            # 用户提供了显式卡片区域 → 用bbox过滤文本块
            product_blocks_list = []
            for cbbox in card_regions:
                product_blocks_list.append({
                    "bbox": cbbox,
                    "blocks": self._filter_blocks_in_region(text_blocks, cbbox),
                    "oe_block": None,
                })
        else:
            # 自动分割 → 空间距离聚类：每个文本块分配给最近的OE号锚点
            clusters = self._cluster_by_spatial_proximity(text_blocks, columns, page_data)
            if clusters:
                product_blocks_list = []
                for anchor_blk, cluster_blocks, _ in clusters:
                    if not cluster_blocks:
                        continue
                    product_blocks_list.append({
                        "bbox": self._compute_cluster_bbox(cluster_blocks),
                        "blocks": cluster_blocks,
                        "oe_block": anchor_blk,
                    })
            else:
                # 回退: 整页当做一个产品
                ps = page_data.get("page_size", [0, 0, 600, 800])
                default_bbox = [0, 0, ps[2] if len(ps) > 2 else 600, ps[3] if len(ps) > 3 else 800]
                product_blocks_list = [{
                    "bbox": default_bbox,
                    "blocks": text_blocks,
                    "oe_block": None,
                }]

        products = []
        for card_idx, pdata in enumerate(product_blocks_list):
            card_bbox = pdata["bbox"]
            card_blocks = pdata["blocks"]
            oe_block = pdata.get("oe_block")

            if not card_blocks:
                continue

            full_text = " ".join(b["text"] for b in card_blocks)
            card_cx = (card_bbox[0] + card_bbox[2]) / 2
            col_id = self._get_column_id(card_cx, columns)

            product = {
                "page": page_num,
                "card_index": card_idx,
                "card_bbox": card_bbox,
                "raw_text": full_text[:500],
            }

            # ── OE号: 卡片内文本直接匹配 ──
            product["oe_number"] = self._extract_oe_number(full_text)

            # ── 品牌: 卡片内检测 + 列顶部继承 ──
            card_brand = self._extract_brand(full_text, card_blocks)
            inherited_brand = column_brand.get(col_id, {})
            if card_brand["value"]:
                product["brand"] = card_brand
            elif inherited_brand.get("value"):
                product["brand"] = {
                    "value": inherited_brand["value"],
                    "confidence": min(0.85, inherited_brand.get("confidence", 0.8)),
                    "method": "column_inherit",
                }
            else:
                product["brand"] = {"value": "", "confidence": 0.0, "method": "none"}

            # ── 车型适配: 列顶部车型 + 卡片内FOR文本 ──
            col_vehicle = column_vehicle.get(col_id, {})
            card_vehicle = self._extract_vehicle(full_text)
            if col_vehicle.get("value"):
                col_val = col_vehicle["value"]
                card_val = card_vehicle.get("value", "")
                # 只有card_val包含车型年份信息时才拼接，避免将描述文本误拼入车型
                has_vehicle_info = bool(
                    card_val and (
                        re.search(r'\b\d{2}\s*[-–—]\s*\d{2,4}\+?\b', card_val)  # 年份范围: 16-20, 14-17
                        or re.search(r'\b\d{2}\+\b', card_val)  # 年份: 18+, 21+
                        or re.search(r'\b\d{4}\s*[-–—]\s*\d{4}\b', card_val)  # 4位年份: 2014-2017
                    )
                )
                if card_val and has_vehicle_info and len(card_val) < len(col_val):
                    combined = f"{col_val} {card_val}"
                else:
                    combined = col_val if col_val else card_val
                product["vehicle_fitment"] = {
                    "value": combined[:120],
                    "confidence": col_vehicle.get("confidence", 0.8),
                    "method": col_vehicle.get("method", "column_top"),
                }
            elif card_vehicle.get("value"):
                product["vehicle_fitment"] = card_vehicle
            else:
                product["vehicle_fitment"] = {"value": "", "confidence": 0.0, "method": "none"}

            # ── 描述字段: 替代 product_name + description ──
            desc_results = self._extract_descriptions(full_text, card_blocks, product, oe_block)
            product["description_1"] = desc_results.get("description_1", {"value": "", "confidence": 0.0, "method": "none"})
            product["description_2"] = desc_results.get("description_2", {"value": "", "confidence": 0.0, "method": "none"})

            # ── 其他字段 ──
            product["description_3"] = self._extract_specs(full_text)
            product["price"] = self._extract_price(full_text)
            product["oem_ref"] = self._extract_oem_ref(full_text)
            product["pack_qty"] = self._extract_pack_qty(full_text)

            # 计算综合置信度
            confidences = [
                v.get("confidence", 0) if isinstance(v, dict) else 0
                for k, v in product.items()
                if k not in ("page", "card_index", "card_bbox", "raw_text", "confidence_avg")
                and isinstance(v, dict) and v.get("value")
            ]
            product["confidence_avg"] = round(sum(confidences) / max(len(confidences), 1), 2)

            products.append(product)

        return products

    # ── 列检测与分组 ──

    def _detect_columns(self, text_blocks: list, page_data: dict) -> dict:
        """
        检测页面列结构。支持:
        - 单栏 (全页一列): {"main": (0, page_w)}
        - 双栏 (左右两页拼一页): {"left": (0, mid), "right": (mid, page_w)}

        策略: 分析文本块的x坐标分布，找到中线分界
        """
        if len(text_blocks) < 4:
            ps = page_data.get("page_size", [600, 800])
            return {"main": (0, ps[0] if len(ps) >= 1 else 600)}

        ps = page_data.get("page_size", [600, 800])
        page_w = ps[0] if len(ps) >= 1 else 600  # 支持 [w,h] 和 [x0,y0,x1,y1] 格式

        # 收集所有文本块的x中心点
        x_centers = []
        for blk in text_blocks:
            bbox = blk.get("bbox", [])
            if len(bbox) >= 4:
                x_centers.append((bbox[0] + bbox[2]) / 2)

        if not x_centers:
            return {"main": (0, page_w)}

        # 统计x分布，寻找中间空白区域
        x_centers.sort()
        mid_point = page_w / 2

        # 计算左半和右半的文本块数量
        left_count = sum(1 for x in x_centers if x < mid_point)
        right_count = sum(1 for x in x_centers if x > mid_point)

        # 如果两边都有足够的文本块，判定为双栏
        total = len(x_centers)
        if left_count >= total * 0.15 and right_count >= total * 0.15:
            # 找到左半和右半各自的最小/最大x，确定分界线
            left_xs = [x for x in x_centers if x < mid_point]
            right_xs = [x for x in x_centers if x > mid_point]
            if left_xs and right_xs:
                split_x = (max(left_xs) + min(right_xs)) / 2
                return {
                    "left": (0, split_x),
                    "right": (split_x, page_w),
                }

        return {"main": (0, page_w)}

    def _get_column_id(self, card_cx: float, columns: dict) -> str:
        """确定卡片中心点属于哪个列"""
        for col_id, (x0, x1) in columns.items():
            if x0 <= card_cx <= x1:
                return col_id
        return list(columns.keys())[0] if columns else "main"

    def _extract_top_vehicle(self, top_blocks: list) -> dict:
        """
        从页面顶部文本块中提取车型适配信息。
        寻找包含 FOR / 适配 关键词的文本块，字号越大概率是标题。
        """
        candidates = []
        for blk in top_blocks:
            text = blk.get("text", "").strip()
            if not text or len(text) < 3:
                continue
            # 跳过品牌名（纯大写短文本）
            if text.upper() in BRAND_DICT_EN or text in BRAND_DICT_CN:
                continue
            # 跳过纯数字/PAGE
            if re.match(r'^PAGE\s*\d', text, re.IGNORECASE):
                continue
            if re.match(r'^\d+$', text):
                continue

            # 检测 FOR / 适配关键词
            for pattern, base_conf in CAR_MODEL_PATTERNS:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    fs = blk.get("font_size_avg", 10)
                    conf = base_conf + 0.02 * (fs / 10)  # 字号大的置信度高
                    candidates.append({
                        "value": text,
                        "confidence": round(min(0.95, conf), 2),
                        "method": "column_top",
                        "font_size": fs,
                    })
                    break

        if candidates:
            return max(candidates, key=lambda c: c["font_size"])
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_top_brand(self, top_blocks: list) -> dict:
        """
        从页面顶部文本块中提取品牌（列级品牌）。
        找字号最大且在品牌词典中的文本。
        """
        best = None
        best_fs = 0
        for blk in top_blocks:
            text = blk.get("text", "").strip()
            fs = blk.get("font_size_avg", 0)

            # 词典直接匹配
            for brand_key, brand_name in {**BRAND_DICT_CN, **BRAND_DICT_EN}.items():
                brand_lower = brand_key.lower()
                text_lower = text.lower()
                if len(brand_lower) <= 3:
                    if not re.search(r'\b' + re.escape(brand_lower) + r'\b', text_lower):
                        continue
                elif brand_lower not in text_lower:
                    continue
                if fs > best_fs:
                    best_fs = fs
                    best = {
                        "value": brand_name,
                        "confidence": 0.88,
                        "method": "column_top",
                    }

        return best or {"value": "", "confidence": 0.0, "method": "none"}

    def _cluster_by_spatial_proximity(self, text_blocks: list, columns: dict, page_data: dict) -> list:
        """
        使用空间距离聚类将文本块分配给最近的OE号锚点。

        策略:
        1. 扫描全页文本分布，找到所有OE号文本块作为聚类锚点
        2. 对每个非OE文本块，计算与同列内各锚点的归一化空间距离
        3. 分配给最近的锚点（距离阈值内），避免"遥远字段"误关联
        4. 返回 [(anchor_block, assigned_blocks, oe_value), ...]

        设计原则: 相邻最近的文本块优先与OE号关联，为后续图片-文字关联做准备。
        """
        if len(text_blocks) < 2:
            return []

        # 第一步: 找到所有OE号锚点
        oe_anchors = []  # [(block_index, oe_value)]
        for i, blk in enumerate(text_blocks):
            text = blk.get("text", "").strip()
            if not text or len(text) < 4:
                continue
            for pattern, _ in OE_PATTERNS:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    value = match.group(0) if match.lastindex is None else match.group()
                    value = value.strip().strip('/').strip()
                    if len(value) >= 4 and not self._is_oe_false_positive(value):
                        oe_anchors.append((i, value))
                        break

        if not oe_anchors:
            return []

        # 计算归一化参数
        ps = page_data.get("page_size", [600, 800])
        page_w = ps[0] if len(ps) >= 1 else 600

        font_sizes = [b.get("font_size_avg", 10) for b in text_blocks if b.get("font_size_avg", 0) > 0]
        avg_font_size = sum(font_sizes) / max(len(font_sizes), 1) if font_sizes else 10
        line_height = max(avg_font_size * 2.0, 15)  # 估算行高

        col_w = page_w / max(len(columns), 1)

        # 第二步: 为每个非锚点文本块找最近的同列锚点
        block_to_anchor = {}

        for i, blk in enumerate(text_blocks):
            if any(i == ai for ai, _ in oe_anchors):
                continue  # 跳过锚点本身

            blk_cx, blk_cy = bbox_center(blk["bbox"])
            blk_col = self._get_column_id(blk_cx, columns)

            best_anchor_idx = None
            best_dist = float("inf")

            for ai, _ in oe_anchors:
                abl = text_blocks[ai]
                ax, ay = bbox_center(abl["bbox"])
                anchor_col = self._get_column_id(ax, columns)

                # 只在同列或单栏模式下分配，避免跨列混淆
                if blk_col != anchor_col and "main" not in [blk_col, anchor_col]:
                    continue

                dx = abs(blk_cx - ax)
                dy = abs(blk_cy - ay)

                # 归一化距离: y用行高归一化, x用30%列宽归一化
                # 水平容差较小，因为同产品文字通常在同一垂直线上
                norm_dx = dx / max(col_w * 0.3, 30)
                norm_dy = dy / max(line_height, 1)
                dist = (norm_dx ** 2 + norm_dy ** 2) ** 0.5

                if dist < best_dist:
                    best_dist = dist
                    best_anchor_idx = ai

            # 距离阈值: 约8行高或2.4个30%列宽（取欧氏距离上限）
            if best_anchor_idx is not None and best_dist < 8.0:
                block_to_anchor[i] = best_anchor_idx

        # 第三步: 构建聚类结果
        clusters = []
        for ai, oe_val in oe_anchors:
            cluster_blocks = [text_blocks[ai]]
            for bi, assigned_ai in block_to_anchor.items():
                if assigned_ai == ai:
                    cluster_blocks.append(text_blocks[bi])
            clusters.append((text_blocks[ai], cluster_blocks, oe_val))

        return clusters

    def _compute_cluster_bbox(self, blocks: list) -> list:
        """计算一组文本块的包围盒"""
        if not blocks:
            return [0, 0, 100, 100]
        x0 = min(b["bbox"][0] for b in blocks)
        y0 = min(b["bbox"][1] for b in blocks)
        x1 = max(b["bbox"][2] for b in blocks)
        y1 = max(b["bbox"][3] for b in blocks)
        # 添加少量边距
        return [x0 - 5, y0 - 2, x1 + 5, y1 + 2]

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
                value = value.strip()
                if len(value) < 3:
                    continue
                # 裁剪产品名称格式：文本中包含 /XX.../ 模式的，截断到第一个产品名之前
                # 避免 "FOR HONDA CIVC 16-20 /Type-R Spoiler(For Sedan)" 这种误匹配
                slash_product_idx = re.search(r'\s/\s*[A-Z]', value)
                if slash_product_idx:
                    value = value[:slash_product_idx.start()].strip()
                if len(value) < 3:
                    continue
                candidates.append({"value": value, "confidence": round(base_conf, 2), "method": "regex"})

        best = self._dedup_candidates(candidates)
        if best:
            return best[0]
        return {"value": "", "confidence": 0.0, "method": "none"}

    def _extract_descriptions(self, text: str, blocks: list, product: dict, oe_block: dict = None) -> dict:
        """
        提取描述字段（描述一、描述二）。

        策略:
        - 从产品卡片文本中，移除已识别字段（OE号、品牌、车型等）
        - 剩余文本按距离OE号排序：最近的文本块是描述一，次近的是描述二
        - 过滤掉页码、纯数字、纯符号、车型文本等无效文本
        - 距离截断：远离OE锚点的文本块不分配到任何描述字段
        """
        # ── 构建排除集合 ──
        # 只排除完整字段值（长文本），不使用单词片段排除（避免"FOR"等常见词误伤描述）
        excluded_full_texts = set()
        for key in ["oe_number", "brand", "vehicle_fitment", "price", "oem_ref"]:
            val = product.get(key, {})
            if isinstance(val, dict) and val.get("value"):
                full_val = val["value"].strip()
                if len(full_val) >= 6:  # 只排除足够长的完整值，避免短词污染
                    excluded_full_texts.add(full_val.lower())

        # ── 车型年份模式（用于过滤纯车型文本块）──
        VEHICLE_YEAR_PATTERNS = [
            # 纯年份/年份范围
            r'^\d{2}\s*[-–—]\s*\d{2,4}\s*\+?\s*$',
            r'^\d{2}\s*[-–—]\s*\d{2,4}\s*[-–—]\s*\d{2,4}\s*$',
            r'^\d{2}\+\s*$',
            # 车型代码 + 年份: 模型代码可含字母和数字
            r'^[A-Z0-9]{2,6}\s+\d{2}\s*[-–—]\s*\d{2,4}\+?\s*$',
            r'^[A-Z0-9]{2,6}\s+\d{2}\+\s*$',
            # 车型代码 + 年份 + 额外年份: "Q50 14-17 18+"
            r'^[A-Z0-9]{2,6}\s+\d{2}\s*[-–—]\s*\d{2,4}\s+\d{2}\+\s*$',
        ]
        # 纯车型代码（常见车系缩写，容易被OCR截断，仅短文本匹配）
        VEHICLE_CODE_BLACKLIST = {
            'CIVC', 'CVC', 'CVIC', 'CIVIC',  # Honda Civic variants
        }

        # ── OE引用模式（描述中引用的其他产品编号）──
        OE_REF_PATTERN = re.compile(
            r'\b([A-Z]{2,4}\s*[-–—]\s*[A-Z0-9]{2,6}\s*[-–—]\s*\d{2,4})/?\b'
        )

        # ── 计算归一化参数 ──
        font_sizes = [b.get("font_size_avg", 10) for b in blocks if b.get("font_size_avg", 0) > 0]
        avg_font_size = sum(font_sizes) / max(len(font_sizes), 1) if font_sizes else 10
        line_height = max(avg_font_size * 2.2, 15)

        # 从文本块中提取候选描述
        desc_parts = []
        oe_val = product.get("oe_number", {}).get("value", "")
        oe_val_clean = oe_val.strip('/').strip() if oe_val else ""

        for blk in blocks:
            txt = blk.get("text", "").strip()
            if not txt:
                continue
            # ── 基础过滤 ──
            if re.match(r'^PAGE\s*\d', txt, re.IGNORECASE):
                continue
            if re.match(r'^[\$\€\¥]', txt):
                continue
            if re.match(r'^\d+$', txt):
                continue
            if re.match(r'^[□\u25a0\u25a1\u2b1b\u2b1c\ufffd]+$', txt):
                continue
            if len(txt) < 3:
                continue

            # ── OE号排除：如果文本块包含OE号，移除OE部分后保留剩余描述文本 ──
            if oe_val_clean and len(oe_val_clean) >= 4:
                if oe_val_clean in txt:
                    # Split block text by lines, remove the OE line, keep description lines
                    sub_lines = txt.split('\n')
                    kept_lines = []
                    for sl in sub_lines:
                        sl = sl.strip()
                        if not sl:
                            continue
                        if oe_val_clean in sl:
                            # Remove OE portion from this line, keep rest if any
                            remainder = sl.replace(oe_val_clean, '').strip(' /-')
                            if remainder and len(remainder) >= 3:
                                kept_lines.append(remainder)
                        else:
                            if len(sl) >= 3:
                                kept_lines.append(sl)
                    if not kept_lines:
                        continue
                    # Process each kept line as a separate description candidate
                    # Distribute Y positions across the original block height so line
                    # grouping can separate them correctly.
                    block_bbox = blk["bbox"]
                    block_h = block_bbox[3] - block_bbox[1]
                    num_kept = len(kept_lines)
                    line_h = block_h / num_kept if num_kept > 0 else blk.get("font_size_avg", 10) * 1.2
                    fs = blk.get("font_size_avg", 10)
                    cx = (block_bbox[0] + block_bbox[2]) / 2
                    for i, line_text in enumerate(kept_lines):
                        if len(line_text) < 3:
                            continue
                        cy = block_bbox[1] + (i + 0.5) * line_h
                        desc_parts.append((line_text, fs, cx, cy))
                    continue  # Skip normal block processing for this block
                elif txt.strip('/').strip() in oe_val_clean:
                    continue

            # ── 排除已被其他字段使用的文本（完整值匹配，不用单词片段）──
            is_excluded = False
            txt_lower = txt.lower()
            for excluded in excluded_full_texts:
                if len(excluded) >= 6:
                    # 文本块与已提取字段值显著重叠 → 排除
                    if excluded in txt_lower or txt_lower in excluded:
                        is_excluded = True
                        break
            if is_excluded:
                continue

            # ── 排除纯车型年份文本 ──
            txt_normalized = txt.strip('/').strip()
            is_vehicle_year = False
            for vp in VEHICLE_YEAR_PATTERNS:
                if re.search(vp, txt_normalized, re.IGNORECASE):
                    is_vehicle_year = True
                    break
            if is_vehicle_year:
                continue

            # ── 排除短车型代码 ──
            if txt_normalized.upper() in VEHICLE_CODE_BLACKLIST:
                continue

            # ── 排除OE引用（描述中引用其他产品编号）──
            # 如果文本块很短(<30字符)且主要是一个OE号引用，排除
            if len(txt) < 35:
                oe_ref_match = OE_REF_PATTERN.search(txt)
                if oe_ref_match:
                    ref_val = oe_ref_match.group(1)
                    # 确保这不是自己的OE号
                    if ref_val.strip('/') != oe_val_clean:
                        # 短文本块主要由OE引用组成 → 排除
                        remaining = txt.replace(oe_ref_match.group(0), '').strip('/').strip()
                        if len(remaining) < 6:
                            continue

            fs = blk.get("font_size_avg", 10)
            cx, cy = bbox_center(blk["bbox"])
            desc_parts.append((txt, fs, cx, cy))

        if not desc_parts:
            return {
                "description_1": {"value": "", "confidence": 0.0, "method": "none"},
                "description_2": {"value": "", "confidence": 0.0, "method": "none"},
            }

        # ── 按距离OE号排序 + 距离截断 ──
        if oe_block:
            ox, oy = bbox_center(oe_block["bbox"])
            desc_parts.sort(key=lambda x: ((x[2] - ox)**2 + (x[3] - oy)**2)**0.5)
            # 距离截断：描述块必须在OE锚点附近（归一化距离 < 3.0倍行高）
            DISTANCE_CUTOFF = line_height * 3.0
            desc_parts = [
                dp for dp in desc_parts
                if abs(dp[3] - oy) < DISTANCE_CUTOFF  # 垂直距离截断
            ]
        else:
            desc_parts.sort(key=lambda x: x[1], reverse=True)

        # ── 分配描述字段（按行分组）──
        # 策略: 按Y坐标将文本块分组为"行"
        #   同一行的所有文本块 → 描述一
        #   下一行的所有文本块 → 描述二
        #   再下一行也归描述二
        if len(desc_parts) >= 2:
            # Sort by Y then X for line grouping
            desc_parts.sort(key=lambda dp: (dp[3], dp[2]))
            # Group into lines by Y proximity
            font_sizes_line = [dp[1] for dp in desc_parts]
            avg_fs_line = sum(font_sizes_line) / len(font_sizes_line) if font_sizes_line else 10
            line_threshold = avg_fs_line * 0.6  # ~0.6x font size : separates lines reliably
            grouped_lines = []
            current_line = [desc_parts[0]]
            for dp in desc_parts[1:]:
                if abs(dp[3] - current_line[-1][3]) < line_threshold:
                    current_line.append(dp)
                else:
                    grouped_lines.append(current_line)
                    current_line = [dp]
            grouped_lines.append(current_line)
            # Within each line, sort left-to-right
            for line in grouped_lines:
                line.sort(key=lambda dp: dp[2])
            # Allocate: line 0 → desc_1, line 1+ → desc_2
            d1_parts = [dp[0] for dp in grouped_lines[0]]
            d2_parts = []
            for line in grouped_lines[1:]:
                d2_parts.extend([dp[0] for dp in line])
        elif len(desc_parts) == 1:
            d1_parts = [desc_parts[0][0]]
            d2_parts = []
        else:
            d1_parts = []
            d2_parts = []

        description_1 = " ".join(d1_parts)[:150] if d1_parts else ""
        description_2 = " ".join(d2_parts)[:200] if d2_parts else ""

        return {
            "description_1": {
                "value": description_1,
                "confidence": 0.70 if description_1 else 0.0,
                "method": "descriptive_blocks",
            },
            "description_2": {
                "value": description_2,
                "confidence": 0.60 if description_2 else 0.0,
                "method": "residual_blocks",
            },
        }

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
        for field in ["brand", "vehicle_fitment", "oe_number",
                       "description_1", "description_2", "description_3",
                       "price", "oem_ref", "pack_qty"]:
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
