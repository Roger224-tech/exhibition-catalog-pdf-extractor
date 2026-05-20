"""
OCR引擎模块
===========
为扫描件PDF页面提供OCR识别，生成与文字型PDF一致的text_blocks结构。

支持两种引擎:
  - paddleocr: PaddleOCR (推荐，中文识别率更高)
  - tesseract: Tesseract OCR (备选，通过pytesseract调用)

用法:
    from ocr_engine import OCREngine
    engine = OCREngine()
    text_blocks = engine.process_page(page_image_array)
"""

import os
import sys
import time
import numpy as np

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


class OCREngine:
    """OCR引擎封装，支持 PaddleOCR 和 Tesseract"""

    def __init__(self, engine: str = "paddleocr", lang: str = "ch"):
        """
        参数:
          engine: "paddleocr" | "tesseract"
          lang: "ch" (中英文) | "en" (英文) | "chinese_cht" (繁体)
        """
        self.engine = engine
        self.lang = lang
        self._ocr = None
        self._available = False
        self._init_engine()

    def _init_engine(self):
        """初始化OCR引擎"""
        if self.engine == "paddleocr":
            self._init_paddleocr()
        elif self.engine == "tesseract":
            self._init_tesseract()

    def _init_paddleocr(self):
        """初始化PaddleOCR (兼容 2.x 和 3.x API)"""
        try:
            from paddleocr import PaddleOCR
            # PaddleOCR 3.x 移除了 use_gpu / use_angle_cls / show_log 参数
            # 2.x 仍然支持这些参数
            import inspect
            sig = inspect.signature(PaddleOCR.__init__)
            params = list(sig.parameters.keys())

            kwargs = {"lang": self.lang}
            # 仅当参数存在时才传入（兼容 2.x）
            if "use_gpu" in params:
                kwargs["use_gpu"] = False
            if "use_angle_cls" in params:
                kwargs["use_angle_cls"] = True
            if "show_log" in params:
                kwargs["show_log"] = False

            self._ocr = PaddleOCR(**kwargs)
            self._available = True
            print("  [OCR] PaddleOCR 初始化成功")
        except ImportError:
            print("  [OCR] PaddleOCR 未安装。请运行: pip install paddlepaddle paddleocr")
            print("  [OCR] 将尝试 Tesseract 备选...")
            self.engine = "tesseract"
            self._init_tesseract()
        except Exception as e:
            print(f"  [OCR] PaddleOCR 初始化失败: {e}")
            self.engine = "tesseract"
            self._init_tesseract()

    def _init_tesseract(self):
        """初始化Tesseract"""
        try:
            import pytesseract
            # 检查tesseract是否可用
            version = pytesseract.get_tesseract_version()
            self._ocr = pytesseract
            self._available = True
            print(f"  [OCR] Tesseract {version} 就绪")
        except ImportError:
            print("  [OCR] pytesseract 未安装。请运行: pip install pytesseract")
            print("  [OCR] 并安装 Tesseract: https://github.com/UB-Mannheim/tesseract/wiki")
            self._available = False
        except Exception as e:
            print(f"  [OCR] Tesseract 不可用: {e}")
            self._available = False

    @property
    def is_available(self) -> bool:
        return self._available

    def process_page(self, page_image: np.ndarray) -> list:
        """
        对单页图像执行OCR，返回text_blocks格式

        参数:
          page_image: numpy array (H, W, 3) RGB图像

        返回:
          text_blocks: [{"text": "识别文本", "bbox": [x0,y0,x1,y1], "confidence": 0.95, ...}, ...]
        """
        if not self._available:
            return []

        if self.engine == "paddleocr":
            return self._process_paddleocr(page_image)
        elif self.engine == "tesseract":
            return self._process_tesseract(page_image)
        return []

    def _process_paddleocr(self, page_image: np.ndarray) -> list:
        """PaddleOCR处理"""
        result = self._ocr.ocr(page_image, cls=True)
        if not result or not result[0]:
            return []

        text_blocks = []
        for line in result[0]:
            bbox_points = line[0]  # [[x,y],[x,y],[x,y],[x,y]]
            text = line[1][0]      # 文本内容
            confidence = line[1][1]  # 置信度

            # 四点转矩形bbox
            xs = [p[0] for p in bbox_points]
            ys = [p[1] for p in bbox_points]
            bbox = [min(xs), min(ys), max(xs), max(ys)]

            text_blocks.append({
                "text": text,
                "bbox": bbox,
                "confidence": round(confidence, 2),
                "font_size_avg": round((bbox[3] - bbox[1]) * 0.8, 1),  # 估算字号
                "fonts": ["ocr"],
                "char_count": len(text),
                "source": "paddleocr",
            })

        return text_blocks

    def _process_tesseract(self, page_image: np.ndarray) -> list:
        """Tesseract OCR处理"""
        import pytesseract
        from PIL import Image

        # numpy array -> PIL Image
        pil_img = Image.fromarray(page_image)

        # 获取详细OCR数据（含位置）
        try:
            data = pytesseract.image_to_data(pil_img, lang="chi_sim+eng", output_type=pytesseract.Output.DICT)
        except Exception:
            # 回退到英文
            data = pytesseract.image_to_data(pil_img, lang="eng", output_type=pytesseract.Output.DICT)

        text_blocks = []
        n = len(data["text"])

        # 合并同一"段落"的词
        current_block = None

        for i in range(n):
            text = data["text"][i].strip()
            if not text:
                if current_block:
                    text_blocks.append(current_block)
                    current_block = None
                continue

            conf = int(data["conf"][i]) / 100.0
            if conf < 0:  # -1表示无置信度
                conf = 0.6

            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            block_num = data["block_num"][i]
            par_num = data["par_num"][i]
            line_num = data["line_num"][i]

            if current_block and current_block.get("line_num") == line_num:
                # 同行追加
                current_block["text"] += " " + text
                current_block["bbox"][0] = min(current_block["bbox"][0], x)
                current_block["bbox"][1] = min(current_block["bbox"][1], y)
                current_block["bbox"][2] = max(current_block["bbox"][2], x + w)
                current_block["bbox"][3] = max(current_block["bbox"][3], y + h)
                current_block["confidence"] = (current_block["confidence"] + conf) / 2
            else:
                if current_block:
                    text_blocks.append(current_block)
                current_block = {
                    "text": text,
                    "bbox": [x, y, x + w, y + h],
                    "confidence": conf,
                    "font_size_avg": round(h * 0.75, 1),
                    "fonts": ["ocr"],
                    "char_count": len(text),
                    "source": "tesseract",
                    "block_num": block_num,
                    "line_num": line_num,
                }

        if current_block:
            text_blocks.append(current_block)

        return [b for b in text_blocks if b["confidence"] > 0.3]


def pdf_page_to_image(doc, page_num: int, dpi: int = 300) -> np.ndarray:
    """
    将PDF页面渲染为numpy图像数组

    参数:
      doc: fitz.Document
      page_num: 0-based 页码
      dpi: 渲染分辨率

    返回:
      numpy array (H, W, 3) RGB
    """
    import fitz
    page = doc[page_num]
    # 计算缩放矩阵
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)

    # 转为numpy array
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:  # RGBA -> RGB
        img = img[:, :, :3]
    elif pix.n == 1:  # Grayscale -> RGB
        img = np.stack([img] * 3, axis=-1)

    return img


# ── 便捷函数 ───────────────────────────────────────

def ocr_scanned_page(doc, page_num: int, engine: OCREngine, dpi: int = 200) -> list:
    """
    对扫描件PDF单页执行OCR

    参数:
      doc: fitz.Document
      page_num: 0-based 页码
      engine: OCREngine实例
      dpi: OCR分辨率 (200足够识别，300更精细但慢)

    返回:
      text_blocks: 与文字型PDF格式一致的文本块列表
    """
    t0 = time.time()
    img = pdf_page_to_image(doc, page_num, dpi=dpi)
    text_blocks = engine.process_page(img)
    elapsed = time.time() - t0
    return text_blocks, elapsed


if __name__ == "__main__":
    # 自检: 列出可用引擎
    print("OCR引擎自检:")
    for e in ["paddleocr", "tesseract"]:
        eng = OCREngine(engine=e)
        status = "[OK] 可用" if eng.is_available else "[FAIL] 不可用"
        print(f"  {e}: {status}")
