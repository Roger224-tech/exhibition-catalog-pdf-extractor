"""
Step 4: 图片-产品关联验证
=========================
将页面中的图片与对应的产品记录自动匹配。

关联策略 (按优先级):
  1. 空间包含: 图片bbox完全在产品卡片bbox内 → 置信度 0.95
  2. 最近距离: 图片中心距离产品卡片中心最近的 → 距离归一化评分
  3. 垂直对齐: 图片与产品文本在垂直方向有重叠 → 置信度 0.80
  4. 位置顺序: 按阅读顺序配对 → 置信度 0.60 (兜底)

用法:
    python step4_image_matching.py ../step3_fields/sample_a_fields.json
    python step4_image_matching.py ../step3_fields/sample_a_fields.json --images-dir ../step1_parsing/sample_a_images/
"""

import json
import os
import sys
import time
import math
import argparse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


# ─── 核心关联算法 ───────────────────────────────────

def match_images_to_products(
    products: list,
    page_images: list,
    page_size: tuple,
    page_num: int,
) -> list:
    """
    将图片与产品关联

    参数:
      products: [{card_bbox: [...], page: N, ...}, ...]
      page_images: [{bbox: [...], ext: "jpeg", xref: N}, ...]
      page_size: (width, height)

    返回:
      matches: [{image_idx, product_idx, confidence, method}, ...]
    """
    # 过滤当前页的产品，并确保每个产品有有效的 card_bbox
    page_products = [
        p for p in products
        if p.get("page") == page_num and p.get("card_bbox") and len(p.get("card_bbox", [])) == 4
    ]
    # 过滤无有效bbox的图片
    valid_images = [
        (i, img) for i, img in enumerate(page_images)
        if img.get("bbox") and len(img.get("bbox", [])) == 4
    ]
    if not page_products or not valid_images:
        return []

    matches = []
    unmatched_images = list(valid_images)

    # ── 策略1: 空间包含 ──
    still_unmatched = []
    for img_idx, img in unmatched_images:
        img_bbox = img.get("bbox", [])
        if not img_bbox or len(img_bbox) < 4:
            still_unmatched.append((img_idx, img))
            continue

        found = False
        for prod in page_products:
            card_bbox = prod.get("card_bbox", [])
            if not card_bbox:
                continue
            if bbox_contains(card_bbox, img_bbox):
                matches.append({
                    "image_idx": img_idx,
                    "product_idx": page_products.index(prod),
                    "confidence": 0.95,
                    "method": "containment",
                })
                found = True
                break
        if not found:
            still_unmatched.append((img_idx, img))
    unmatched_images = still_unmatched

    # ── 策略2: 最近距离 ──
    still_unmatched = []
    for img_idx, img in unmatched_images:
        img_bbox = img.get("bbox", [])
        if not img_bbox or len(img_bbox) < 4:
            still_unmatched.append((img_idx, img))
            continue

        img_center = bbox_center(img_bbox)
        best_prod_idx = -1
        best_dist = float("inf")
        best_score = 0

        for prod in page_products:
            card_bbox = prod.get("card_bbox", [])
            if not card_bbox or len(card_bbox) < 4:
                continue
            prod_center = bbox_center(card_bbox)
            dist = euclidean(img_center, prod_center)
            max_dim = max(page_size) if page_size else 1000
            normalized_dist = dist / max_dim
            score = max(0.0, 1.0 - normalized_dist * 3)

            if score > best_score:
                best_score = score
                best_dist = dist
                best_prod_idx = page_products.index(prod)

        if best_prod_idx >= 0 and best_score > 0.3:
            matches.append({
                "image_idx": img_idx,
                "product_idx": best_prod_idx,
                "confidence": round(best_score, 2),
                "method": "nearest_distance",
                "distance_px": round(best_dist, 1),
            })
        else:
            still_unmatched.append((img_idx, img))
    unmatched_images = still_unmatched

    # ── 策略3: 垂直对齐 ──
    still_unmatched2 = []
    for img_idx, img in unmatched_images:
        img_bbox = img.get("bbox", [])
        if not img_bbox or len(img_bbox) < 4:
            continue

        best_prod_idx = -1
        best_overlap = 0
        for prod in page_products:
            card_bbox = prod.get("card_bbox", [])
            if not card_bbox or len(card_bbox) < 4:
                continue
            overlap = vertical_overlap(img_bbox, card_bbox)
            if overlap > best_overlap:
                best_overlap = overlap
                best_prod_idx = page_products.index(prod)

        if best_overlap > 0.4:
            matches.append({
                "image_idx": img_idx,
                "product_idx": best_prod_idx,
                "confidence": 0.80,
                "method": "vertical_alignment",
                "overlap_ratio": round(best_overlap, 2),
            })
        else:
            still_unmatched2.append((img_idx, img))
    unmatched_images = still_unmatched2

    # ── 策略4: 按阅读顺序兜底 ──
    # 按y坐标排序图片和产品，顺序配对
    sorted_imgs = sorted(unmatched_images, key=lambda x: (x[1].get("bbox", [0, 0, 0, 0]) or [0, 0, 0, 0])[1])
    # 找一个还没有图片的产品
    matched_prod_indices = {m["product_idx"] for m in matches}
    unmatched_prods = [p for i, p in enumerate(page_products) if i not in matched_prod_indices]
    sorted_prods = sorted(unmatched_prods, key=lambda p: (p.get("card_bbox", [0, 0, 0, 0]) or [0, 0, 0, 0])[1])

    for i, (img_idx, img) in enumerate(sorted_imgs):
        if i < len(sorted_prods):
            prod_idx = page_products.index(sorted_prods[i])
            matches.append({
                "image_idx": img_idx,
                "product_idx": prod_idx,
                "confidence": 0.60,
                "method": "reading_order",
            })

    return matches


# ─── 几何工具函数 ───────────────────────────────────

def bbox_center(bbox) -> tuple:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def bbox_contains(parent: list, child: list) -> bool:
    """child bbox 是否完全在 parent bbox 内"""
    return (
        child[0] >= parent[0]
        and child[1] >= parent[1]
        and child[2] <= parent[2]
        and child[3] <= parent[3]
    )


def euclidean(p1: tuple, p2: tuple) -> float:
    return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)


def vertical_overlap(b1: list, b2: list) -> float:
    """两个bbox在垂直方向的重叠比例"""
    y_overlap_start = max(b1[1], b2[1])
    y_overlap_end = min(b1[3], b2[3])
    overlap_h = max(0, y_overlap_end - y_overlap_start)
    min_h = min(b1[3] - b1[1], b2[3] - b2[1])
    if min_h == 0:
        return 0
    return overlap_h / min_h


# ─── 报告生成 ───────────────────────────────────────

def print_matching_report(matches_by_page: dict, total_images: int, total_products: int):
    """打印图片关联报告"""
    print("\n" + "=" * 60)
    print("  图片-产品关联报告")
    print("=" * 60)
    print(f"  总产品数:        {total_products}")
    print(f"  总图片数:        {total_images}")

    all_matches = []
    for page, matches in matches_by_page.items():
        all_matches.extend(matches)

    print(f"  已关联图片:      {len(all_matches)}")

    if not all_matches:
        print("  (无图片可关联)")
        print("=" * 60 + "\n")
        return

    # 按方法统计
    from collections import Counter
    method_counts = Counter(m["method"] for m in all_matches)
    print(f"\n  关联方法分布:")
    for method, cnt in method_counts.most_common():
        print(f"    {method}: {cnt} ({cnt/max(len(all_matches),1)*100:.0f}%)")

    # 置信度分布
    conf_buckets = {"高(≥90%)": 0, "中(70-89%)": 0, "低(<70%)": 0}
    for m in all_matches:
        c = m["confidence"]
        if c >= 0.9:
            conf_buckets["高(≥90%)"] += 1
        elif c >= 0.7:
            conf_buckets["中(70-89%)"] += 1
        else:
            conf_buckets["低(<70%)"] += 1

    print(f"\n  置信度分布:")
    for bucket, cnt in conf_buckets.items():
        pct = cnt / max(len(all_matches), 1) * 100
        print(f"    {bucket}: {cnt} ({pct:.0f}%)")

    # 低置信度警告
    low_count = conf_buckets["低(<70%)"]
    if low_count / max(len(all_matches), 1) > 0.15:
        print(f"\n  [WARN] 低置信度关联比例过高 ({low_count/max(len(all_matches),1)*100:.0f}%)，建议检查")

    print("=" * 60 + "\n")


# ─── 主入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Step 4: 图片-产品关联验证")
    parser.add_argument("input_path", help="Step3 字段抽取结果 JSON")
    parser.add_argument("--images-dir", help="图片目录 (从Step1导出)")
    parser.add_argument("-o", "--output", default="step4_matching", help="输出目录")
    args = parser.parse_args()

    input_path = Path(args.input_path)
    if not input_path.exists():
        print(f"错误: 文件不存在 {input_path}")
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(args.output, exist_ok=True)

    products = data.get("products", [])
    total_products = len(products)

    # 尝试从 Step1 解析数据中加载图片信息
    images_by_page = {}

    if args.images_dir:
        # 图片目录模式
        img_dir = Path(args.images_dir)
        if img_dir.exists():
            for img_file in img_dir.glob("*"):
                # 文件名格式: p001_xref123.jpeg
                name = img_file.stem
                parts = name.split("_")
                if parts[0].startswith("p"):
                    page_num = int(parts[0][1:])
                    if page_num not in images_by_page:
                        images_by_page[page_num] = []
                    images_by_page[page_num].append({
                        "filename": img_file.name,
                        "path": str(img_file),
                        "bbox": [],  # 无位置信息
                    })

    # 也尝试从数据中读取图片信息（如果存在）
    source_file = data.get("source_file", "")
    if source_file:
        source_dir = Path(source_file).parent
        parsed_json = source_dir.parent / "step1_parsing" / f"{Path(source_file).stem}_parsed.json"
        if parsed_json.exists():
            print(f"从Step1结果加载图片坐标: {parsed_json}")
            with open(parsed_json, "r", encoding="utf-8") as f:
                parsed_data = json.load(f)
            for page_data in parsed_data.get("pages", []):
                pn = page_data["page_num"]
                if pn not in images_by_page or not images_by_page.get(pn):
                    images_by_page[pn] = []
                for img in page_data.get("images", []):
                    images_by_page[pn].append(img)

    # 按页执行关联
    t0 = time.time()
    matches_by_page = {}
    total_images = 0

    for page_num in set(p.get("page", 0) for p in products):
        page_images = images_by_page.get(page_num, [])
        total_images += len(page_images)
        page_size = (600, 800)  # 默认
        # 尝试从产品数据中获取页面尺寸
        for p in products:
            if p.get("page") == page_num:
                cb = p.get("card_bbox", [])
                if cb:
                    # 用card bbox推算，不精确但够用
                    page_size = (cb[2] + 200, cb[3] + 200)
                    break

        matches = match_images_to_products(products, page_images, page_size, page_num)
        if matches:
            matches_by_page[page_num] = matches

    total_time = time.time() - t0

    print_matching_report(matches_by_page, total_images, total_products)

    # 保存结果
    prefix = Path(data.get("source_file", input_path.stem)).stem
    json_out = os.path.join(args.output, f"{prefix}_matching.json")
    output_data = {
        "source_file": data.get("source_file", str(input_path)),
        "total_products": total_products,
        "total_images": total_images,
        "total_matches": sum(len(v) for v in matches_by_page.values()),
        "matching_time_seconds": round(total_time, 2),
        "matches_by_page": {str(k): v for k, v in matches_by_page.items()},
    }
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2, default=str)
    print(f"结果已保存: {json_out}")


if __name__ == "__main__":
    main()
