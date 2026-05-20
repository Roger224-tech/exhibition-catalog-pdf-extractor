"""
Step 2: 版面分析验证
===================
对PDF页面进行版面分析，识别产品卡片区域，分离内容区块和无效区域（页眉页脚）。

策略:
  A. MinerU CLI (优先) — 命令行调用 magic-pdf，解析输出的 *_middle.json
  B. pdfplumber + 空间聚类 (后备) — 基于文本块坐标的卡片边界检测

用法:
    python step2_layout_analysis.py ../step1_parsing/sample_a_parsed.json
    python step2_layout_analysis.py ../test_pdfs/sample_a.pdf --method pdfplumber
"""

import json
import os
import sys
import time
import argparse
import subprocess
import shutil
from pathlib import Path
from collections import defaultdict
import math

import yaml

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


# ─── 工具函数 ───────────────────────────────────────

def load_parsed_json(json_path: str) -> dict:
    """加载 Step 1 的解析结果"""
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def bbox_iou(b1, b2) -> float:
    """计算两个 bounding box 的 IOU"""
    x1 = max(b1[0], b2[0])
    y1 = max(b1[1], b2[1])
    x2 = min(b1[2], b2[2])
    y2 = min(b1[3], b2[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    area2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    return inter / max(area1 + area2 - inter, 1)


def bbox_center(b) -> tuple:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


# ─── 方法A: MinerU ──────────────────────────────────

def run_mineru(pdf_path: str, output_dir: str, timeout_sec: int = 300) -> dict:
    """
    调用 MinerU CLI 处理PDF

    前置条件: pip install magic-pdf
    输出: output_dir/{pdf_name}/{pdf_name}_middle.json
    """
    pdf_name = Path(pdf_path).stem
    cmd = ["magic-pdf", "-p", pdf_path, "-o", output_dir]

    print(f"  执行 MinerU: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr, "output": result.stdout}

        # 查找输出文件
        mineru_out = Path(output_dir) / pdf_name
        middle_json = mineru_out / f"{pdf_name}_middle.json"
        model_json = mineru_out / f"{pdf_name}_model.json"
        md_file = mineru_out / f"{pdf_name}.md"

        return {
            "success": True,
            "middle_json": str(middle_json) if middle_json.exists() else None,
            "model_json": str(model_json) if model_json.exists() else None,
            "md_file": str(md_file) if md_file.exists() else None,
            "stdout": result.stdout,
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"超时 ({timeout_sec}秒)"}
    except FileNotFoundError:
        return {
            "success": False,
            "error": "MinerU 未安装。请运行: pip install magic-pdf",
            "fallback": "请使用 --method pdfplumber",
        }


def parse_mineru_output(mineru_result: dict) -> list:
    """
    解析 MinerU 输出，提取每页的产品卡片区域

    MinerU middle.json 结构:
    {
      "pdf_info": [...],  # 每页一个元素
      "_parse_type": "pdf"
    }
    每页包含: blocks[{type, bbox, lines[{spans[{content, bbox}]}]}]
    type: text | image | table
    """
    if not mineru_result.get("model_json"):
        return []

    with open(mineru_result["model_json"], "r", encoding="utf-8") as f:
        data = json.load(f)

    pages_cards = []
    # MinerU model.json 是每页的结构化数据
    for page_info in data:
        page_data = {
            "page_num": page_info.get("page_num", 0),
            "page_size": page_info.get("page_size", [0, 0]),
            "blocks": [],
            "card_regions": [],
            "invalid_regions": [],
        }

        # 分类所有区块
        all_blocks = []
        for block in page_info.get("blocks", []):
            block_type = block.get("type", "text")
            bbox = block.get("bbox", [])
            text = ""
            if block_type == "text":
                texts = []
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        texts.append(span.get("content", ""))
                text = "".join(texts)
            all_blocks.append({
                "type": block_type,
                "bbox": bbox,
                "text": text,
            })

        page_data["blocks"] = all_blocks

        # 检测页眉页脚（靠近页面顶部/底部）
        _, _, _, page_h = page_data["page_size"]
        hf_margin = 50
        for blk in all_blocks:
            bbox = blk["bbox"]
            if bbox and len(bbox) == 4:
                if bbox[3] < hf_margin or bbox[1] > page_h - hf_margin:
                    page_data["invalid_regions"].append(bbox)

        # 基于空白间距聚类检测产品卡片
        page_data["card_regions"] = detect_cards_by_gap(all_blocks, page_data["page_size"])

        pages_cards.append(page_data)

    return pages_cards


# ─── 方法B: pdfplumber + 规则 ───────────────────────

def analyze_with_rules(pdf_path: str) -> dict:
    """
    基于规则的版面分析 (不依赖 MinerU)
    使用 pdfplumber 提取文字+线条，通过空白间距识别卡片边界
    """
    import pdfplumber

    result = {"pages": [], "method": "pdfplumber_rules"}

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_w = page.width
            page_h = page.height

            # 提取文字及坐标
            words = page.extract_words(keep_blank_chars=True, x_tolerance=3)

            # 提取线条（可能是卡片分隔线）
            lines = page.lines or []

            # 按y坐标排序
            words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))

            # 检测页眉/页脚
            hf_margin = 50
            content_words = [
                w for w in words_sorted
                if w["top"] > hf_margin and w["bottom"] < page_h - hf_margin
            ]

            # ── 基于y轴间距做行聚类 ──
            row_clusters = cluster_by_gap(
                content_words,
                axis="y",
                gap_threshold=20,  # 行间距阈值 (pt)
            )

            # ── 在每个行簇内，基于x轴间距做列聚类 ──
            card_regions = []
            for row in row_clusters:
                col_clusters = cluster_by_gap(
                    row,
                    axis="x",
                    gap_threshold=40,  # 列间距阈值 (pt)
                )
                for col in col_clusters:
                    if len(col) < 2:
                        continue  # 单个文字不算卡片
                    bbox = words_cluster_bbox(col)
                    card_regions.append(bbox)

            # 合并嵌套区域
            card_regions = merge_nested_boxes(card_regions)

            result["pages"].append({
                "page_num": page_num + 1,
                "page_size": [page_w, page_h],
                "word_count": len(content_words),
                "card_regions": card_regions,
                "line_count": len(lines),
            })

    return result


def cluster_by_gap(words: list, axis: str, gap_threshold: float) -> list:
    """
    基于间距的词簇聚类
    axis: "y" 按垂直位置聚类 (行) | "x" 按水平位置聚类 (列)
    """
    if not words:
        return []

    if axis == "y":
        key_start = "top"
        key_end = "bottom"
    else:
        key_start = "x0"
        key_end = "x1"

    sorted_words = sorted(words, key=lambda w: w[key_start])
    clusters = []
    current = [sorted_words[0]]

    for w in sorted_words[1:]:
        prev = current[-1]
        gap = w[key_start] - prev[key_end]
        if gap > gap_threshold:
            clusters.append(current)
            current = [w]
        else:
            current.append(w)
    clusters.append(current)
    return clusters


def words_cluster_bbox(words: list) -> list:
    """计算一组词的包围盒"""
    x0 = min(w["x0"] for w in words)
    y0 = min(w["top"] for w in words)
    x1 = max(w["x1"] for w in words)
    y1 = max(w["bottom"] for w in words)
    return [x0, y0, x1, y1]


def merge_nested_boxes(boxes: list, iou_threshold: float = 0.8) -> list:
    """合并高度重叠的嵌套box"""
    if not boxes:
        return boxes
    merged = []
    used = set()
    for i, b1 in enumerate(boxes):
        if i in used:
            continue
        group = b1
        for j, b2 in enumerate(boxes):
            if j <= i or j in used:
                continue
            if bbox_iou(b1, b2) > iou_threshold:
                group = [
                    min(group[0], b2[0]), min(group[1], b2[1]),
                    max(group[2], b2[2]), max(group[3], b2[3]),
                ]
                used.add(j)
        merged.append(group)
        used.add(i)
    return merged


def detect_cards_by_gap(blocks: list, page_size: list) -> list:
    """基于空白间距检测产品卡片区域"""
    text_blocks = [b for b in blocks if b["type"] == "text" and b.get("bbox")]
    if len(text_blocks) < 3:
        return [page_size[:]]  # 整页作为一个卡片

    # 按y坐标排序后检测大的垂直间距
    sorted_blocks = sorted(text_blocks, key=lambda b: b["bbox"][1])
    gaps = []
    for i in range(1, len(sorted_blocks)):
        gap = sorted_blocks[i]["bbox"][1] - sorted_blocks[i - 1]["bbox"][3]
        gaps.append((gap, i))

    # 取最大的几个间隙作为分隔
    mean_gap = sum(g[0] for g in gaps) / max(len(gaps), 1)
    splits = [0] + sorted([g[1] for g in gaps if g[0] > mean_gap * 2]) + [len(sorted_blocks)]

    cards = []
    for i in range(len(splits) - 1):
        group = sorted_blocks[splits[i]:splits[i + 1]]
        if group:
            x0 = min(b["bbox"][0] for b in group)
            y0 = min(b["bbox"][1] for b in group)
            x1 = max(b["bbox"][2] for b in group)
            y1 = max(b["bbox"][3] for b in group)
            cards.append([x0, y0, x1, y1])

    return cards


# ─── 报告生成 ───────────────────────────────────────

def print_layout_report(result: dict):
    """打印版面分析报告"""
    print("\n" + "=" * 60)
    print(f"  版面分析报告 (方法: {result.get('method', 'unknown')})")
    print("=" * 60)

    total_cards = 0
    total_invalid = 0
    for p in result.get("pages", []):
        cards = len(p.get("card_regions", []))
        total_cards += cards
        total_invalid += len(p.get("invalid_regions", []))

    print(f"  总页数:          {len(result.get('pages', []))}")
    print(f"  检测到产品卡片:  {total_cards}")
    print(f"  无效区域标记:    {total_invalid}")
    print("-" * 60)

    for p in result.get("pages", []):
        cards = p.get("card_regions", [])
        words = p.get("word_count", 0)
        print(f"  页码 {p['page_num']}:  {len(cards)} 卡片, {words} 有效词")

    print("=" * 60 + "\n")


# ─── 主入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Step 2: 版面分析验证")
    parser.add_argument("input_path", help="PDF文件路径 或 Step1解析JSON路径")
    parser.add_argument("-m", "--method", choices=["mineru", "pdfplumber", "auto"],
                        default="auto", help="版面分析方法 (默认auto: 优先MinerU)")
    parser.add_argument("-o", "--output", default="step2_layout", help="输出目录")
    args = parser.parse_args()

    input_path = Path(args.input_path)
    os.makedirs(args.output, exist_ok=True)

    result = None

    # 判断输入类型
    if input_path.suffix.lower() == ".pdf":
        pdf_path = str(input_path.resolve())
    elif input_path.suffix.lower() == ".json":
        # 从Step1结果中提取PDF路径
        parsed = load_parsed_json(str(input_path))
        pdf_path = parsed.get("filepath", "")
        if not pdf_path or not Path(pdf_path).exists():
            print("错误: JSON中未找到有效的PDF路径，请直接提供PDF文件")
            sys.exit(1)
    else:
        print("错误: 输入必须是 .pdf 或 .json 文件")
        sys.exit(1)

    method = args.method
    if method == "auto":
        # 检查 MinerU 是否可用
        method = "pdfplumber"
        if shutil.which("magic-pdf"):
            method = "mineru"
        else:
            print("MinerU 未安装，回退到 pdfplumber 方法")

    t0 = time.time()

    if method == "mineru":
        print(f"\n使用 MinerU 分析: {pdf_path}")
        mineru_result = run_mineru(pdf_path, args.output)
        if mineru_result["success"]:
            pages = parse_mineru_output(mineru_result)
            result = {"pages": pages, "method": "mineru", "mineru_output": mineru_result}
        else:
            print(f"  MinerU 失败: {mineru_result.get('error')}")
            print(f"  回退到 pdfplumber 方法...")
            method = "pdfplumber"

    if method == "pdfplumber":
        print(f"\n使用 pdfplumber + 规则分析: {pdf_path}")
        result = analyze_with_rules(pdf_path)

    if result is None:
        print("错误: 分析失败，无结果")
        sys.exit(1)

    result["analysis_time_seconds"] = round(time.time() - t0, 3)

    print_layout_report(result)

    # 保存结果
    prefix = Path(pdf_path).stem
    json_out = os.path.join(args.output, f"{prefix}_layout.json")
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"结果已保存: {json_out}")


if __name__ == "__main__":
    main()
