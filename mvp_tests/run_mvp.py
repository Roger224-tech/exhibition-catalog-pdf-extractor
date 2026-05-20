#!/usr/bin/env python
"""
MVP验证一键运行入口
====================
按顺序执行 Step 1-5，并将所有输出结果保存到指定目录。

用法:
    # 单文件处理
    python run_mvp.py test_pdfs/sample_a.pdf

    # 批量处理 (文件夹)
    python run_mvp.py test_pdfs/

    # 指定输出目录
    python run_mvp.py test_pdfs/sample_a.pdf -o mvp_results

    # 仅运行指定步骤
    python run_mvp.py test_pdfs/sample_a.pdf --steps 1,2
    python run_mvp.py test_pdfs/sample_a.pdf --steps 3-5

输出目录结构:
    mvp_results/
    ├── sample_a/
    │   ├── step1_parsed.json        # PDF解析结果
    │   ├── sample_a_images/         # 导出的图片
    │   ├── step2_layout.json        # 版面分析结果
    │   ├── step3_fields.json        # 字段抽取结果
    │   ├── step4_matching.json      # 图片关联结果
    │   ├── sample_a_output.xlsx     # 最终Excel
    │   └── sample_a_report.json     # 处理报告
    └── _batch_summary.json          # 批量汇总
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# Python 脚本目录
SCRIPT_DIR = Path(__file__).parent.resolve()

SCRIPTS = {
    "1": ("step1_pdf_parse.py", "PDF解析"),
    "2": ("step2_layout_analysis.py", "版面分析"),
    "3": ("step3_field_extraction.py", "字段抽取"),
    "4": ("step4_image_matching.py", "图片关联"),
    "5": ("step5_e2e_pipeline.py", "端到端+Excel"),
}


def parse_steps(steps_str: str) -> list:
    """解析步骤参数，如 '1,2,4-5' -> ['1','2','4','5']"""
    steps = []
    for part in steps_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            steps.extend(str(i) for i in range(int(start), int(end) + 1))
        else:
            steps.append(part)
    return sorted(set(steps), key=int)


def run_step(step_id: str, pdf_path: str, output_dir: str, step_output: str) -> bool:
    """运行单个步骤"""
    script_name, step_desc = SCRIPTS[step_id]
    script_path = SCRIPT_DIR / script_name

    if not script_path.exists():
        print(f"  ✗ 脚本不存在: {script_path}")
        return False

    print(f"\n{'─'*50}")
    print(f"  Step {step_id}: {step_desc}")
    print(f"{'─'*50}")

    t0 = time.time()

    # 构建参数
    if step_id == "1":
        cmd = [sys.executable, str(script_path), pdf_path, "-o", step_output]
    elif step_id == "2":
        # Step2 使用Step1的JSON结果
        pdf_name = Path(pdf_path).stem
        step1_json = Path(step_output) / f"{pdf_name}_parsed.json"
        if step1_json.exists():
            cmd = [sys.executable, str(script_path), str(step1_json), "-o", step_output]
        else:
            print(f"  Step1 结果不存在，跳过Step2: {step1_json}")
            return False
    elif step_id == "3":
        # Step3 使用Step2结果 (如果存在)，否则用Step1
        pdf_name = Path(pdf_path).stem
        step2_json = Path(step_output) / f"{pdf_name}_layout.json"
        step1_json = Path(step_output) / f"{pdf_name}_parsed.json"
        input_json = step2_json if step2_json.exists() else step1_json
        cmd = [sys.executable, str(script_path), str(input_json), "-o", step_output]
    elif step_id == "4":
        # Step4 使用Step3结果
        pdf_name = Path(pdf_path).stem
        step3_json = Path(step_output) / f"{pdf_name}_fields.json"
        if step3_json.exists():
            cmd = [sys.executable, str(script_path), str(step3_json), "-o", step_output]
            # 如果有图片目录则附加
            img_dir = Path(step_output) / f"{pdf_name}_images"
            if img_dir.exists():
                cmd.extend(["--images-dir", str(img_dir)])
        else:
            print(f"  Step3 结果不存在，跳过Step4: {step3_json}")
            return False
    elif step_id == "5":
        cmd = [
            sys.executable, str(script_path), pdf_path,
            "-o", str(Path(step_output).parent),  # Step5输出到上级目录
            "--image-mode", "path",
        ]
    else:
        return False

    try:
        result = subprocess.run(cmd, capture_output=False, timeout=600)
        elapsed = time.time() - t0
        if result.returncode == 0:
            print(f"  ✓ Step {step_id} 完成 (耗时 {elapsed:.1f}s)")
            return True
        else:
            print(f"  ✗ Step {step_id} 失败 (返回码: {result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        print(f"  ✗ Step {step_id} 超时 (600s)")
        return False
    except Exception as e:
        print(f"  ✗ Step {step_id} 异常: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="MVP验证一键运行 — 执行全部或指定步骤",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run_mvp.py test_pdfs/sample_a.pdf
  python run_mvp.py test_pdfs/
  python run_mvp.py test_pdfs/sample_a.pdf --steps 1,2
  python run_mvp.py test_pdfs/sample_a.pdf --steps 3-5
  python run_mvp.py test_pdfs/sample_a.pdf -o results --skip-existing
        """,
    )
    parser.add_argument("input_path", help="PDF文件或文件夹路径")
    parser.add_argument("-o", "--output", default="mvp_results", help="输出根目录 (默认: mvp_results)")
    parser.add_argument("--steps", default="1-5", help="运行步骤，如 '1,2,3-5' (默认: 1-5)")
    parser.add_argument("--skip-existing", action="store_true", help="跳过已有输出的步骤")
    parser.add_argument("--list-steps", action="store_true", help="列出可用步骤并退出")
    args = parser.parse_args()

    if args.list_steps:
        print("\n可用步骤:")
        for sid, (sname, sdesc) in SCRIPTS.items():
            print(f"  Step {sid}: {sdesc} ({sname})")
        return

    steps = parse_steps(args.steps)
    input_path = Path(args.input_path)

    # 收集PDF文件
    pdf_files = []
    if input_path.is_dir():
        pdf_files = sorted(input_path.glob("*.pdf"))
        if not pdf_files:
            print(f"错误: 目录 '{input_path}' 中无PDF文件")
            sys.exit(1)
    elif input_path.suffix.lower() == ".pdf":
        if not input_path.exists():
            print(f"错误: 文件不存在 '{input_path}'")
            sys.exit(1)
        pdf_files = [input_path]
    else:
        print(f"错误: '{input_path}' 不是PDF文件或目录")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  MVP 技术验证")
    print(f"  文件数: {len(pdf_files)}")
    print(f"  步骤:   {', '.join(f'Step {s}' for s in steps)}")
    print(f"  输出:   {args.output}")
    print(f"{'='*60}")

    total_start = time.time()
    all_pass = True

    for pdf_path in pdf_files:
        pdf_name = pdf_path.stem
        step_output = os.path.join(args.output, pdf_name)
        os.makedirs(step_output, exist_ok=True)

        print(f"\n{'▶'*30}")
        print(f"  处理文件: {pdf_path.name}")
        print(f"{'▶'*30}")

        for step_id in steps:
            success = run_step(step_id, str(pdf_path.resolve()), step_output, step_output)
            if not success:
                all_pass = False
                if step_id in ("1", "5"):  # 关键步骤失败则中断
                    print(f"\n  ⚠ 关键步骤 Step {step_id} 失败，中断处理")
                    break

    total_elapsed = time.time() - total_start
    print(f"\n{'='*60}")
    print(f"  MVP验证{'完成' if all_pass else '部分完成'}")
    print(f"  总耗时: {total_elapsed:.1f} 秒")
    print(f"  输出目录: {os.path.abspath(args.output)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
