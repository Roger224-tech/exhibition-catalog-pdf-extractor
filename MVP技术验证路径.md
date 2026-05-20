# 展会目录AI提取工具 —— MVP技术验证路径

## 验证目标（一句话）

用 3 份真实汽配PDF，跑通 **PDF → 版面分析 → 字段抽取 → 图片关联 → Excel输出** 全链路，确认核心AI管线在真实数据上能达到验收标准。

## 验证范围（做什么 / 不做什么）

| 做 | 不做 |
|----|------|
| 文字型PDF解析 + 提取 | 扫描件OCR（Phase 2） |
| 命令行脚本验证，无GUI | 图形界面（Phase 2） |
| 3份不同版式的汽配目录 | 批量处理、模板系统 |
| 输出Excel + 裁切图片 | 人工校验界面 |
| 规则+NLP字段抽取 | 模型微调（需要标注数据） |
| 图片-产品自动关联 | 多语言（仅中英文） |

## 技术栈（验证用，非最终版）

```
PDF解析:    PyMuPDF (fitz)      — 文字提取 + 图片导出 + 坐标获取
版面分析:    MinerU magic-pdf     — 将PDF页面转为结构化markdown/json
           （备选: pdfplumber + 规则引擎）
OCR预留:    PaddleOCR            — Phase 2启用，本次仅预留接口
字段抽取:    spaCy + 正则规则     — NER抽取OE号、品牌、车型等
            （备选: GLiNER 小参数量NER模型）
图片关联:    OpenCV + 空间距离    — 基于bounding box距离匹配
Excel输出:  openpyxl             — 支持图片嵌入
```

## 技术验证步骤（5步，预计2周）

### 步骤 0：环境准备

```bash
# 创建独立conda/pip环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 核心依赖
pip install pymupdf          # PDF解析
pip install magic-pdf        # MinerU CLI（版面分析）
pip install spacy            # NLP
python -m spacy download zh_core_web_sm
python -m spacy download en_core_web_sm
pip install opencv-python    # 图像处理
pip install pillow           # 图片操作
pip install openpyxl         # Excel生成
pip install pandas           # 数据处理

# 备选/辅助
pip install paddleocr        # Phase 2 OCR（可选预装）
pip install gliner           # 轻量NER备选（可选预装）
```

### 步骤 1：PDF解析能力验证（Day 1-2）

**目标**：确认能从汽配PDF中正确提取文本、位置坐标和图片。

**验证脚本** → 见 `tests/step1_pdf_parse.py`

**验证点**：
- [ ] 文字型PDF：所有文字块按坐标排序，无乱码、无截断
- [ ] 扫描件PDF（预留）：识别为图像型，标记为"需OCR"
- [ ] 图片提取：PDF内嵌图片能导出为独立jpg/png文件
- [ ] 坐标精度：文字块bounding box与PDF实际位置一致（用人眼对照）
- [ ] 性能：100页PDF解析 ≤ 30秒（不含OCR）

**关键代码骨架**：

```python
import fitz  # pymupdf

def parse_pdf(pdf_path: str) -> dict:
    """解析PDF，返回每页的文本块和图片"""
    doc = fitz.open(pdf_path)
    pages_data = []

    for page_num, page in enumerate(doc):
        # 1. 判断是否扫描件（文字量极少 = 扫描件）
        text = page.get_text("text")
        is_scanned = len(text.strip()) < 50

        # 2. 提取文本块（含坐标）
        blocks = []
        for block in page.get_text("dict")["blocks"]:
            if block["type"] == 0:  # 文本块
                for line in block["lines"]:
                    text_span = "".join([s["text"] for s in line["spans"]])
                    bbox = line["bbox"]  # (x0, y0, x1, y1)
                    blocks.append({
                        "text": text_span,
                        "bbox": bbox,
                        "font_size": line["spans"][0]["size"],
                        "font_name": line["spans"][0]["font"],
                    })

        # 3. 提取图片
        images = []
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            img_bytes = base_image["image"]
            # 获取图片在页面中的位置
            img_rects = page.get_image_rects(xref)
            for rect in img_rects:
                images.append({
                    "xref": xref,
                    "bbox": list(rect),  # 转换为可序列化格式
                    "ext": base_image["ext"],
                    "width": base_image["width"],
                    "height": base_image["height"],
                })

        pages_data.append({
            "page_num": page_num + 1,
            "is_scanned": is_scanned,
            "blocks": blocks,
            "images": images,
        })

    return {"filename": pdf_path, "total_pages": len(doc), "pages": pages_data}
```

**通过标准**：
- 3份测试PDF的文本提取完整率（人眼对比）≥ 95%
- 图片导出成功率 ≥ 95%


### 步骤 2：版面分析验证（Day 2-4）

**目标**：用MinerU将PDF页面转为结构化区块，确认能正确识别产品卡片边界。

**验证脚本** → 见 `tests/step2_layout_analysis.py`

**验证点**：
- [ ] MinerU能正确处理中英文混排的汽配页面
- [ ] 产品卡片边界识别准确（多卡片页面，如2×3布局）
- [ ] 文本块、图片块正确分类
- [ ] 跨页产品（产品描述跨两页）能标记
- [ ] 页眉页脚/广告/无关水印被正确归类为"非产品内容"

**两条验证路径（并行测试，选最优）**：

**路径A：MinerU CLI**

```bash
# MinerU 命令行处理
magic-pdf -p input.pdf -o output_dir

# 输出结构：
# output_dir/
#   ├── input_middle.json   ← 版面分析中间结果（版面区块树）
#   ├── input_model.json    ← 最终结果（文本+图+位置+阅读顺序）
#   ├── input.md            ← markdown格式
#   └── images/             ← 提取的图片
```

**路径B：pdfplumber + 自建规则（MinerU效果不佳时的退路）**

```python
import pdfplumber

def rule_based_layout(pdf_path: str, page_num: int) -> list:
    """基于规则的卡片边界检测"""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_num]

        # 1. 提取所有水平分隔线（可能是卡片边界）
        lines = page.lines  # 需要 pdfplumber 识别

        # 2. 检查是否有明显的网格/卡片结构
        #    通过空白间隙、分隔线、标题字体大小变化检测
        words = page.extract_words(keep_blank_chars=True)

        # 3. 按y坐标聚类 → 识别卡片行
        #    按x坐标聚类 → 识别卡片列
        #    输出每个"卡片"的bounding box

        return card_regions
```

**通过标准**：
- 产品卡片区域识别准确率 ≥ 80%（对比人眼标注）
- 每页处理时间 ≤ 2秒
- 页眉/页脚/广告识别为无效区域


### 步骤 3：字段抽取验证（Day 4-6）

**目标**：从结构化文本块中准确提取OE号、产品名、品牌、车型等汽配字段。

**验证脚本** → 见 `tests/step3_field_extraction.py`

**验证点**：
- [ ] OE号/产品编号：正则 + NER，准确率 ≥ 85%
- [ ] 品牌/制造商：NER + 品牌词典匹配
- [ ] 车型适配：正则（"车型" "适用" + 宝马3系 E90等模式）
- [ ] 规格参数：关键词触发（尺寸/材质/重量等）
- [ ] 字段缺失时置信度正确标记为低
- [ ] 每个字段输出置信度分数

**字段提取策略矩阵**：

```
字段            正则       NER      词典匹配    上下文位置
──────────────────────────────────────────────────────
OE号/产品编号    ★★★      ★★       ★           ★★
产品名称         ★        ★★       ★           ★★★
品牌/制造商      ★        ★★       ★★★         ★★
车型适配         ★★★      ★★       ★★          ★
规格参数         ★★★      ★        ★           ★★
价格             ★★★      ★        ★           ★
原厂参考号       ★★★      ★        ★           ★
每包数量         ★★★       -        ★           ★
```

**核心规则示例**：

```python
import re
import spacy

# OE号模式（汽配领域核心正则）
OE_PATTERNS = [
    r'\b[A-Z]{1,4}[\s-]?\d{4,10}\b',           # BMW-345267
    r'\b\d{4,8}[\s-]?\d{4,8}\b',               # 12345-67890
    r'\b[A-Z]{2,3}\d{6,12}\b',                 # L3219876
    r'\bO\.?E\.?\s*[Nn][oO]\.?:?\s*([\w-]+)',  # OE No.: XXX
]

# 品牌词典（汽配常见品牌，可扩展）
BRAND_DICT = {
    "bosch", "zf", "continental", "denso", "valeo",
    "hella", "mahle", "skf", "trw", "delphi",
    "博世", "采埃孚", "大陆", "电装", "法雷奥",
    "海拉", "马勒", "舍弗勒", "天合", "德尔福",
}

# 车型模式
CAR_MODEL_PATTERNS = [
    r'(宝马|BMW|奔驰|Benz|奥迪|Audi)\s*[\w\d]+\s*(E\d{2,3})?',
    r'(适用于?|适配|For|Fit\s*for)\s*.+',
    r'\d{4}\s*[-~]\s*\d{4}',  # 年份范围 2005-2012
]

def extract_fields(text_blocks: list, page_images: list) -> dict:
    """
    输入：版面分析后的文本块列表（含坐标）
    输出：结构化字段 + 置信度
    """
    results = {}
    full_text = " ".join([b["text"] for b in text_blocks])

    # ---- OE号抽取 ----
    oe_candidates = []
    for pattern in OE_PATTERNS:
        for match in re.finditer(pattern, full_text):
            oe_candidates.append({
                "value": match.group(),
                "confidence": min(0.95, 0.7 + 0.05 * len(match.group())),
                "method": "regex",
            })
    # 去重 + 排序
    results["oe_number"] = deduplicate_and_rank(oe_candidates)

    # ---- 品牌抽取 ----
    brand_matches = []
    for brand in BRAND_DICT:
        if brand.lower() in full_text.lower():
            brand_matches.append({
                "value": brand,
                "confidence": 0.90,
                "method": "dictionary",
            })
    results["brand"] = brand_matches

    # ---- 车型适配 ----
    car_matches = []
    for pattern in CAR_MODEL_PATTERNS:
        for match in re.finditer(pattern, full_text):
            car_matches.append({
                "value": match.group(),
                "confidence": 0.75,
                "method": "regex",
            })
    results["vehicle_fitment"] = car_matches

    # ---- 规格参数 (尺寸/材质等) ----
    spec_patterns = [
        (r'(尺寸|Size|规格).*?(\d+\.?\d*\s*[mMcM]{1,2})', 0.80),
        (r'(材质|Material).*?([\u4e00-\u9fff]+)', 0.70),
        (r'(重量|Weight).*?(\d+\.?\d*\s*(kg|g|KG|G))', 0.85),
    ]
    # ... 类似逻辑

    # ---- NER增强（对品牌、产品名） ----
    nlp = spacy.load("zh_core_web_sm")  # 或 en_core_web_sm
    doc = nlp(full_text)
    for ent in doc.ents:
        if ent.label_ in ("ORG", "PRODUCT"):
            # 补充到对应字段
            pass

    return results
```

**通过标准**：
- OE号提取准确率 ≥ 85%（对比人工标注）
- 品牌识别准确率 ≥ 80%
- 每页处理时间 ≤ 3秒


### 步骤 4：图片-产品关联验证（Day 5-7）

**目标**：将提取的图片与对应产品正确匹配。

**验证脚本** → 见 `tests/step4_image_matching.py`

**关联策略（优先级从高到低）**：

```
策略1: 空间包含 —— 图片bbox完全在某个产品卡片bbox内
策略2: 最近距离 —— 图片中心点距哪个产品文字块中心最近
策略3: 上下文位置 —— 图片上方/下方的文字最可能描述该图片
策略4: 页码+序号 —— 同页内按阅读顺序自动编号
```

```python
import cv2
import numpy as np

def match_images_to_products(products: list, images: list, page_size: tuple) -> list:
    """
    核心关联算法
    
    products: [{"id": 1, "text": "...", "bbox": (x0,y0,x1,y1)}, ...]
    images:   [{"id": "img1", "bbox": (x0,y0,x1,y1)}, ...]
    """
    matches = []
    
    for img in images:
        img_center = ((img["bbox"][0] + img["bbox"][2]) / 2,
                       (img["bbox"][1] + img["bbox"][3]) / 2)
        
        best_product = None
        best_score = 0
        best_method = ""
        
        for prod in products:
            prod_bbox = prod["bbox"]
            
            # 策略1: 包含关系（最高置信）
            if bbox_contains(prod_bbox, img["bbox"]):
                score = 0.95
                method = "containment"
            
            # 策略2: 距离计算
            else:
                prod_center = ((prod_bbox[0] + prod_bbox[2]) / 2,
                               (prod_bbox[1] + prod_bbox[3]) / 2)
                distance = np.sqrt((img_center[0] - prod_center[0])**2 +
                                   (img_center[1] - prod_center[1])**2)
                # 归一化距离（相对于页面尺寸）
                normalized_dist = distance / max(page_size)
                score = max(0, 1 - normalized_dist * 3)  # 距离越远分数越低
                method = "nearest_distance"
            
            # 策略3: 垂直相邻（图片在上，产品在下，或反之）
            vertical_overlap = bbox_vertical_overlap(img["bbox"], prod_bbox)
            if vertical_overlap > 0.5 and score < 0.8:
                score = 0.80
                method = "vertical_alignment"
            
            if score > best_score:
                best_score = score
                best_product = prod["id"]
                best_method = method
        
        matches.append({
            "image_id": img["id"],
            "product_id": best_product,
            "confidence": round(best_score, 2),
            "method": best_method,
        })
    
    return matches
```

**通过标准**：
- 图片-产品关联准确率 ≥ 90%
- 每页关联计算 ≤ 0.5秒
- 低置信度匹配（< 70%）比例 ≤ 15%


### 步骤 5：端到端集成 + Excel输出（Day 7-10）

**目标**：串联步骤1-4，从PDF生成最终Excel文件。

**验证脚本** → 见 `tests/step5_e2e_pipeline.py`

```python
def pipeline(input_pdf: str, output_dir: str) -> dict:
    """
    全链路处理：
    PDF → 解析 → 版面分析 → 字段抽取 → 图片关联 → Excel
    """
    report = {
        "file": input_pdf,
        "total_pages": 0,
        "total_products": 0,
        "total_images": 0,
        "errors": [],
        "processing_time_seconds": 0,
    }
    
    start_time = time.time()
    
    # Step 1: PDF解析
    parsed = parse_pdf(input_pdf)
    report["total_pages"] = parsed["total_pages"]
    
    # Step 2-4: 逐页处理
    all_products = []
    for page in parsed["pages"]:
        if page["is_scanned"]:
            report["errors"].append(f"Page {page['page_num']}: 扫描件，跳过")
            continue
        
        # Step 2: 版面分析 → 产品卡片
        cards = analyze_layout(page)
        
        # Step 3: 字段抽取
        for card in cards:
            fields = extract_fields(card["blocks"])
            product = {
                "page": page["page_num"],
                "card_index": card["index"],
                **fields,
                "confidence_avg": avg_confidence(fields),
            }
            all_products.append(product)
        
        # Step 4: 图片关联
        image_matches = match_images_to_products(cards, page["images"])
        # 将图片关联信息写入对应product
        
    report["total_products"] = len(all_products)
    report["total_images"] = sum(len(p["images"]) for p in parsed["pages"])
    
    # Step 5: 导出Excel
    excel_path = export_to_excel(all_products, output_dir)
    report["output_excel"] = excel_path
    
    report["processing_time_seconds"] = time.time() - start_time
    return report


def export_to_excel(products: list, output_dir: str) -> str:
    """生成最终Excel文件"""
    import openpyxl
    from openpyxl.utils import get_column_letter
    from openpyxl.drawing.image import Image as XLImage
    from PIL import Image as PILImage
    import io
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "产品目录"
    
    # 表头
    HEADERS = [
        "页码", "产品编号/OE号", "产品名称", "品牌/制造商",
        "车型适配", "规格参数", "简要描述", "价格", "原厂参考号",
        "每包数量", "产品图片", "AI置信度", "备注"
    ]
    for col, header in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = openpyxl.styles.Font(bold=True)
        cell.fill = openpyxl.styles.PatternFill("solid", fgColor="D9E1F2")
    
    # 数据行
    for row_idx, product in enumerate(products, 2):
        ws.cell(row=row_idx, column=1, value=product.get("page"))
        ws.cell(row=row_idx, column=2, value=format_field(product, "oe_number"))
        ws.cell(row=row_idx, column=3, value=format_field(product, "product_name"))
        ws.cell(row=row_idx, column=4, value=format_field(product, "brand"))
        ws.cell(row=row_idx, column=5, value=format_field(product, "vehicle_fitment"))
        ws.cell(row=row_idx, column=6, value=format_field(product, "specs"))
        ws.cell(row=row_idx, column=7, value=format_field(product, "description"))
        ws.cell(row=row_idx, column=8, value=format_field(product, "price"))
        ws.cell(row=row_idx, column=9, value=format_field(product, "oem_ref"))
        ws.cell(row=row_idx, column=10, value=format_field(product, "pack_qty"))
        
        # 插入缩略图
        if product.get("image_path"):
            thumb = create_thumbnail(product["image_path"], max_size=(120, 120))
            img = XLImage(thumb)
            img.width, img.height = thumb.size
            ws.add_image(img, f"K{row_idx}")
        
        # 置信度
        conf_cell = ws.cell(row=row_idx, column=12, 
                           value=f"{product.get('confidence_avg', 0):.0%}")
        # 低置信度红色标记
        if product.get("confidence_avg", 0) < 0.70:
            conf_cell.fill = openpyxl.styles.PatternFill("solid", fgColor="FFC7CE")
    
    # 调整列宽
    for col in range(1, len(HEADERS) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 18
    
    output_path = os.path.join(output_dir, "output.xlsx")
    wb.save(output_path)
    return output_path
```

**通过标准**：
- 端到端流程无报错
- Excel字段对齐正确，无数据错位
- 图片正确嵌入或路径引用
- 100页PDF总处理 ≤ 5分钟


## 验证数据集要求

### 最少3份测试PDF（体现版式多样性）

| PDF编号 | 版式类型 | 页数 | 说明 |
|---------|---------|------|------|
| PDF-A | 表格型 | 10-20页 | 标准产品列表表格，类似Excel布局 |
| PDF-B | 卡片型（2×2） | 10-20页 | 每页4个产品卡片的图文混排 |
| PDF-C | 自由排版型 | 5-10页 | 图文自由排列，无固定结构 |

> 如暂无真实汽配目录，可先用同类产品目录（如电子产品、五金工具）PDF替代测试。

### 每份PDF的人工标注（Ground Truth）

为验证准确率，需对每份PDF标注：
- 产品边界（每个产品从哪行到哪行）
- 每个字段的正确值（OE号、品名、品牌等）
- 图片与产品的正确对应关系

**标注工作量估算**：每份PDF约 2-3 小时（可由非技术人员完成）


## 验收判定矩阵

| 指标 | 通过阈值 | 测量方法 |
|------|---------|---------|
| 文字型PDF文本提取完整率 | ≥ 95% | 人眼对比 |
| 产品卡片边界识别 | ≥ 80% | vs 人工标注 |
| OE号提取准确率 | ≥ 85% | vs 人工标注 |
| 品牌识别准确率 | ≥ 80% | vs 人工标注 |
| 图片-产品关联准确率 | ≥ 90% | vs 人工标注 |
| 100页处理时间 | ≤ 5分钟 | 计时 |
| Excel输出完整率 | ≥ 95%（无字段错位） | 人眼检查 |

**判定规则**：
- 全部指标达标 → 进入Phase 2（GUI开发 + 扫描件OCR）
- 3项以上不达标 → 调整技术方案（换模型/换规则策略）
- 1-2项不达标 → 针对性优化后复测


## 验证产出物

完成MVP验证后，应交付以下文件：

```
mvp_results/
├── README.md                    # 验证总结报告
├── step1_parsing/
│   ├── extracted_text/          # 提取的原始文本
│   └── extracted_images/        # 导出的图片
├── step2_layout/
│   ├── mineru_output/           # MinerU原始输出
│   └── card_regions.json       # 检测到的产品卡片坐标
├── step3_fields/
│   └── extracted_fields.json   # AI提取的字段（含置信度）
├── step4_matching/
│   └── image_product_map.json  # 图片-产品关联关系
├── step5_output/
│   └── output.xlsx             # 最终Excel
└── ground_truth/
    └── annotations.json        # 人工标注数据
```

## 关键决策点

| 决策 | 触发条件 | 选项 |
|------|---------|------|
| MinerU vs 自建规则 | MinerU卡片识别 < 70% | 改用pdfplumber + 自定义规则 |
| spaCy vs GLiNER | spaCy NER在汽配术语上F1 < 0.7 | 试用GLiNER零样本NER |
| Tesseract vs PaddleOCR | Phase 2扫描件测试 | PaddleOCR优先（离线中文更强） |
| 规则为主 vs 模型为主 | 3份PDF版式差异大 | 规则差异大则需微调模型 |
| 继续开发 vs 调整方案 | 3项以上不达标 | 重新评估AI策略 |
