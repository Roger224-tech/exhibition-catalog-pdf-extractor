"""
Step 1: PDF解析能力验证
========================
验证 PyMuPDF 对汽配PDF的文字提取、坐标获取、图片导出的能力。
区分文字型PDF和扫描件，对扫描件自动启用OCR。

用法:
    python step1_pdf_parse.py ../test_pdfs/sample_a.pdf
    python step1_pdf_parse.py ../test_pdfs/sample_a.pdf --ocr paddleocr
    python step1_pdf_parse.py ../test_pdfs/  # 批量处理文件夹
"""

import fitz  # pymupdf
import json
import os
import sys
import time
import argparse
import hashlib
from pathlib import Path
from PIL import Image
import io

# 修复Windows GBK控制台编码问题
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


# ─── 配置 ───────────────────────────────────────────
TEXT_MIN_LENGTH = 50       # 少于此字符数判定为扫描件
IMAGE_MIN_WIDTH = 50       # 忽略过小图片
IMAGE_MIN_HEIGHT = 50
OUTPUT_DIR = "step1_parsing"


# ─── 核心解析函数 ───────────────────────────────────

def parse_pdf(pdf_path: str, ocr_engine=None) -> dict:
    """解析单个PDF文件，返回结构化数据

    参数:
      pdf_path: PDF文件路径
      ocr_engine: OCREngine实例 (可选，用于扫描件OCR)
    """
    t0 = time.time()
    doc = fitz.open(pdf_path)
    result = {
        "filename": os.path.basename(pdf_path),
        "filepath": str(Path(pdf_path).resolve()),
        "total_pages": len(doc),
        "file_size_mb": round(os.path.getsize(pdf_path) / (1024 * 1024), 2),
        "is_encrypted": doc.is_encrypted,
        "pdf_version": doc.metadata.get("format", "unknown"),
        "pages": [],
        "summary": {"text_pages": 0, "scanned_pages": 0, "ocr_pages": 0, "total_text_blocks": 0, "total_images": 0},
    }

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_data = _parse_page(doc, page, page_num, ocr_engine)
        result["pages"].append(page_data)

        if page_data.get("ocr_applied"):
            result["summary"]["ocr_pages"] += 1
        if page_data["is_scanned"] and not page_data.get("ocr_applied"):
            result["summary"]["scanned_pages"] += 1
        else:
            result["summary"]["text_pages"] += 1
        result["summary"]["total_text_blocks"] += len(page_data["text_blocks"])
        result["summary"]["total_images"] += len(page_data["images"])

    result["parse_time_seconds"] = round(time.time() - t0, 3)
    result["avg_parse_time_per_page"] = round(result["parse_time_seconds"] / max(len(doc), 1), 3)
    return result


def _parse_page(doc: fitz.Document, page: fitz.Page, page_num: int, ocr_engine=None) -> dict:
    """解析单页内容"""
    page_w = page.rect.width
    page_h = page.rect.height

    # 1. 提取纯文本（用于判断是否扫描件）
    full_text = page.get_text("text")
    text_length = len(full_text.strip())
    is_scanned = text_length < TEXT_MIN_LENGTH

    # 2. 提取文本块（含坐标、字体信息）
    text_blocks = []
    block_dict = page.get_text("dict")
    for block in block_dict.get("blocks", []):
        if block["type"] == 0:  # 文本类型
            block_text_parts = []
            block_fonts = set()
            block_sizes = []
            block_bbox = None

            for line in block.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    line_text += span["text"]
                    block_fonts.add(span.get("font", ""))
                    block_sizes.append(span.get("size", 0))
                if line_text.strip():
                    block_text_parts.append(line_text.strip())

                # 扩展bbox
                line_bbox = line["bbox"]
                if block_bbox is None:
                    block_bbox = list(line_bbox)
                else:
                    block_bbox[0] = min(block_bbox[0], line_bbox[0])
                    block_bbox[1] = min(block_bbox[1], line_bbox[1])
                    block_bbox[2] = max(block_bbox[2], line_bbox[2])
                    block_bbox[3] = max(block_bbox[3], line_bbox[3])

            full_block_text = " ".join(block_text_parts)
            if full_block_text.strip() and block_bbox:
                text_blocks.append({
                    "text": full_block_text,
                    "bbox": block_bbox,  # [x0, y0, x1, y1]
                    "font_size_avg": round(sum(block_sizes) / max(len(block_sizes), 1), 1),
                    "fonts": list(block_fonts),
                    "char_count": len(full_block_text),
                })

    # 3. 扫描件OCR处理
    ocr_applied = False
    ocr_time = 0
    if is_scanned and ocr_engine is not None and ocr_engine.is_available:
        try:
            from ocr_engine import ocr_scanned_page
            ocr_blocks, ocr_time = ocr_scanned_page(doc, page_num, ocr_engine, dpi=200)
            if ocr_blocks:
                # OCR坐标需要缩放到PDF坐标 (OCR在200dpi图像上做，PDF坐标基于72dpi)
                scale_ratio = 72.0 / 200.0
                for b in ocr_blocks:
                    b["bbox"] = [v * scale_ratio for v in b["bbox"]]
                text_blocks = ocr_blocks
                ocr_applied = True
                print(f"    第{page_num+1}页 OCR: 识别 {len(ocr_blocks)} 个文本块 ({ocr_time:.1f}s)")
        except Exception as e:
            print(f"    第{page_num+1}页 OCR失败: {e}")

    # 3. 提取图片（含位置、尺寸）
    images = []
    img_list = page.get_images(full=True)
    for img_info in img_list:
        xref = img_info[0]
        try:
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            img_w = base_image["width"]
            img_h = base_image["height"]

            # 忽略过小图片
            if img_w < IMAGE_MIN_WIDTH or img_h < IMAGE_MIN_HEIGHT:
                continue

            # 图片在页面中的位置
            img_rects = page.get_image_rects(xref)
            for rect in img_rects:
                bbox = list(rect)
                # 转换为相对于页面尺寸的比例坐标
                rel_bbox = [
                    round(bbox[0] / page_w, 4),
                    round(bbox[1] / page_h, 4),
                    round(bbox[2] / page_w, 4),
                    round(bbox[3] / page_h, 4),
                ]
                images.append({
                    "xref": xref,
                    "bbox": bbox,           # 绝对坐标
                    "bbox_rel": rel_bbox,   # 相对坐标
                    "width_px": img_w,
                    "height_px": img_h,
                    "ext": base_image["ext"],
                    "size_bytes": len(img_bytes),
                    "md5": hashlib.md5(img_bytes).hexdigest(),
                })
        except Exception as e:
            images.append({"xref": xref, "error": str(e)})

    return {
        "page_num": page_num + 1,
        "page_size": [page_w, page_h],
        "is_scanned": is_scanned,
        "ocr_applied": ocr_applied,
        "ocr_time_seconds": round(ocr_time, 2),
        "text_length": len(full_text.strip()),
        "text_blocks": text_blocks,
        "images": images,
        "image_count": len(images),
        "block_count": len(text_blocks),
    }


# ─── 图片导出 ───────────────────────────────────────

def export_images(doc: fitz.Document, pages_data: list, output_base: str, filename_prefix: str):
    """导出PDF中所有图片为独立文件"""
    img_dir = os.path.join(output_base, f"{filename_prefix}_images")
    os.makedirs(img_dir, exist_ok=True)

    exported = []
    for page_data in pages_data:
        for img in page_data.get("images", []):
            if "error" in img:
                continue
            try:
                xref = img["xref"]
                base_image = doc.extract_image(xref)
                ext = base_image["ext"]

                img_name = f"p{page_data['page_num']:03d}_xref{xref}.{ext}"
                img_path = os.path.join(img_dir, img_name)

                with open(img_path, "wb") as f:
                    f.write(base_image["image"])

                exported.append({
                    "page": page_data["page_num"],
                    "filename": img_name,
                    "path": img_path,
                    "size_kb": round(len(base_image["image"]) / 1024, 1),
                })
            except Exception as e:
                exported.append({"page": page_data["page_num"], "xref": img["xref"], "error": str(e)})

    return img_dir, exported


# ─── 报告生成 ───────────────────────────────────────

def print_report(result: dict, img_dir: str, img_count: int):
    """打印解析报告到控制台"""
    s = result["summary"]
    print("\n" + "=" * 60)
    print(f"  PDF解析报告: {result['filename']}")
    print("=" * 60)
    print(f"  文件大小:        {result['file_size_mb']} MB")
    print(f"  PDF版本:         {result['pdf_version']}")
    print(f"  总页数:          {result['total_pages']}")
    print(f"  文字型页面:      {s['text_pages']}")
    print(f"  扫描件页面:      {s.get('scanned_pages', 0)}")
    print(f"  OCR处理页面:     {s.get('ocr_pages', 0)}")
    print(f"  文本块总数:      {s['total_text_blocks']}")
    print(f"  图片总数:        {s['total_images']}")
    print(f"  导出图片:        {img_count} (→ {img_dir})")
    print(f"  解析耗时:        {result['parse_time_seconds']} 秒")
    print(f"  平均每页:        {result['avg_parse_time_per_page']} 秒")
    print("-" * 60)

    # 逐页详情
    print(f"\n  {'页码':<6} {'类型':<10} {'字符数':<10} {'文本块':<8} {'图片':<6}")
    print("  " + "-" * 44)
    for p in result["pages"]:
        if p.get("ocr_applied"):
            ptype = "OCR扫描件"
        elif p["is_scanned"]:
            ptype = "扫描件"
        else:
            ptype = "文字型"
        print(f"  {p['page_num']:<6} {ptype:<10} {p['text_length']:<10} {p['block_count']:<8} {p['image_count']:<6}")

    # 警告
    warnings = []
    if s.get("scanned_pages", 0) > 0 and s.get("ocr_pages", 0) == 0:
        warnings.append(f"[WARN] {s['scanned_pages']} 页为扫描件，使用 --ocr paddleocr 启用OCR")
    elif s.get("ocr_pages", 0) > 0:
        warnings.append(f"[INFO] {s['ocr_pages']} 页已完成OCR识别")
    if s["total_images"] == 0:
        warnings.append("[WARN] 未检测到图片")
    if result["parse_time_seconds"] > result["total_pages"] * 2:
        warnings.append("[WARN] 解析速度偏慢")

    if warnings:
        print(f"\n  [WARN] 注意事项:")
        for w in warnings:
            print(f"    {w}")

    print("\n" + "=" * 60 + "\n")


def save_results(result: dict, img_dir: str, exported_images: list, output_dir: str):
    """保存解析结果到JSON"""
    os.makedirs(output_dir, exist_ok=True)
    prefix = Path(result["filename"]).stem

    # 完整解析结果（不包含图片二进制数据）
    json_path = os.path.join(output_dir, f"{prefix}_parsed.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    # 图片导出清单
    img_list_path = os.path.join(output_dir, f"{prefix}_images.json")
    with open(img_list_path, "w", encoding="utf-8") as f:
        json.dump(exported_images, f, ensure_ascii=False, indent=2)

    # 摘要报告
    summary_path = os.path.join(output_dir, f"{prefix}_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"PDF解析验证报告\n")
        f.write(f"{'='*40}\n")
        f.write(f"文件: {result['filename']}\n")
        f.write(f"总页数: {result['total_pages']}\n")
        f.write(f"文字型页面: {result['summary']['text_pages']}\n")
        f.write(f"扫描件页面: {result['summary']['scanned_pages']}\n")
        f.write(f"文本块总数: {result['summary']['total_text_blocks']}\n")
        f.write(f"图片总数: {result['summary']['total_images']}\n")
        f.write(f"解析耗时: {result['parse_time_seconds']} 秒\n")

    return json_path


# ─── 主入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Step 1: PDF解析验证")
    parser.add_argument("input_path", help="PDF文件或文件夹路径")
    parser.add_argument("-o", "--output", default=OUTPUT_DIR, help="输出目录")
    parser.add_argument("--no-images", action="store_true", help="不导出图片")
    parser.add_argument("--ocr", choices=["paddleocr", "tesseract"], help="OCR引擎（扫描件PDF必需）")
    args = parser.parse_args()

    input_path = Path(args.input_path)
    pdf_files = []

    if input_path.is_dir():
        pdf_files = sorted(input_path.glob("*.pdf"))
        if not pdf_files:
            print(f"错误: 目录 '{input_path}' 中未找到PDF文件")
            sys.exit(1)
        print(f"批量模式: 找到 {len(pdf_files)} 个PDF文件")
    elif input_path.suffix.lower() == ".pdf":
        pdf_files = [input_path]
    else:
        print(f"错误: '{input_path}' 不是PDF文件")
        sys.exit(1)

    # 初始化OCR引擎
    ocr_engine = None
    if args.ocr:
        from ocr_engine import OCREngine
        print(f"初始化 OCR 引擎: {args.ocr}")
        ocr_engine = OCREngine(engine=args.ocr)
        if not ocr_engine.is_available:
            print("[WARN] OCR引擎不可用，扫描件页面将跳过文本提取")
            ocr_engine = None

    for pdf_path in pdf_files:
        if not pdf_path.exists():
            print(f"跳过不存在的文件: {pdf_path}")
            continue

        print(f"\n正在解析: {pdf_path.name} ...")
        doc = fitz.open(str(pdf_path))

        result = parse_pdf(str(pdf_path), ocr_engine=ocr_engine)

        img_dir = ""
        exported = []
        if not args.no_images:
            prefix = Path(pdf_path).stem
            img_dir, exported = export_images(doc, result["pages"], args.output, prefix)

        doc.close()

        print_report(result, img_dir, len(exported))
        json_path = save_results(result, img_dir, exported, args.output)
        print(f"结果已保存: {json_path}")


if __name__ == "__main__":
    main()
