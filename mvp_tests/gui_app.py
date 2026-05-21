#!/usr/bin/env python3
"""
Interactive GUI for MVP testing of the PDF Auto Parts Catalog Extractor.
Left: PDF page viewer with product card overlay
Center: Product list (Treeview)
Right: Field editor with manual correction capability
"""

import json
import os
import re
import sys
import time
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from collections import OrderedDict

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ── Optional imports (all should be available) ──
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
    print("WARNING: PyMuPDF (fitz) not available - PDF rendering disabled")

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None
    print("WARNING: Pillow not available - PDF rendering disabled")

try:
    from step3_field_extraction import FieldExtractor
    from step4_image_matching import match_images_to_products
    from step5_e2e_pipeline import export_to_excel as step5_export_to_excel
    from ocr_engine import OCREngine
except ImportError as e:
    print(f"WARNING: Pipeline modules not fully available: {e}")

# ═════════════════════════════════════════════════════
#  Constants
# ═════════════════════════════════════════════════════

FIELD_DEFINITIONS = [
    {"key": "brand",          "label": "品牌/制造商",    "editable": True},
    {"key": "vehicle_fitment","label": "车型适配",       "editable": True},
    {"key": "oe_number",      "label": "产品编号/OE号", "editable": True},
    {"key": "description_1",  "label": "描述一",         "editable": True},
    {"key": "description_2",  "label": "描述二",         "editable": True},
    {"key": "description_3",  "label": "描述三/规格",    "editable": True},
    {"key": "price",          "label": "价格",           "editable": True},
    {"key": "oem_ref",        "label": "OEM参考号",      "editable": True},
    {"key": "pack_qty",       "label": "每包数量",       "editable": True},
]

# Fields that form a "text group" — when OCR detects multiple lines over
# these fields' region, lines are auto-distributed in order.
TEXT_GROUP_FIELDS = ["oe_number", "description_1", "description_2", "description_3"]

# ── Field config & template persistence ──
FIELD_TEMPLATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "field_templates.json")

DEFAULT_FIELD_DEFINITIONS = [
    {"key": "brand",          "label": "品牌/制造商",    "editable": True},
    {"key": "vehicle_fitment","label": "车型适配",       "editable": True},
    {"key": "oe_number",      "label": "产品编号/OE号", "editable": True},
    {"key": "description_1",  "label": "描述一",         "editable": True},
    {"key": "description_2",  "label": "描述二",         "editable": True},
    {"key": "description_3",  "label": "描述三/规格",    "editable": True},
    {"key": "price",          "label": "价格",           "editable": True},
    {"key": "oem_ref",        "label": "OEM参考号",      "editable": True},
    {"key": "pack_qty",       "label": "每包数量",       "editable": True},
]


def load_templates():
    """Load all templates from file. Returns (templates_dict, active_name)."""
    if os.path.exists(FIELD_TEMPLATES_PATH):
        try:
            with open(FIELD_TEMPLATES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            templates = data.get("templates", {})
            active = data.get("active_template", "")
            # Validate: each template must be a non-empty list with key/label
            valid = {}
            for name, fields in templates.items():
                if isinstance(fields, list) and all(
                    isinstance(f, dict) and "key" in f and "label" in f for f in fields
                ):
                    valid[name] = fields
            if valid:
                return valid, active if active in valid else ""
        except Exception:
            pass

    # Migration: try old field_config.json
    OLD_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "field_config.json")
    if os.path.exists(OLD_CONFIG):
        try:
            with open(OLD_CONFIG, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > 0 and all(
                isinstance(f, dict) and "key" in f and "label" in f for f in data
            ):
                templates = {"默认模板": data}
                save_templates(templates, "默认模板")
                return templates, "默认模板"
        except Exception:
            pass

    return {}, ""


def save_templates(templates: dict, active_name: str):
    """Save all templates to file."""
    try:
        with open(FIELD_TEMPLATES_PATH, "w", encoding="utf-8") as f:
            json.dump({"active_template": active_name, "templates": templates},
                      f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def load_active_template():
    """Load the currently active template field definitions. Returns list or None."""
    templates, active = load_templates()
    if active and active in templates:
        return templates[active]
    return None


def save_active_template(definitions: list):
    """Save field definitions as a template (legacy path for auto-save)."""
    templates, _ = load_templates()
    templates["默认模板"] = definitions
    return save_templates(templates, "默认模板")


# Load active template on module init
_custom = load_active_template()
if _custom:
    FIELD_DEFINITIONS = _custom
else:
    FIELD_DEFINITIONS = list(DEFAULT_FIELD_DEFINITIONS)

CONFIDENCE_COLORS = {
    "high":   "#34C759",  # >= 0.8  Apple绿
    "medium": "#FF9500",  # >= 0.6  Apple橙
    "low":    "#FF3B30",  # < 0.6   Apple红
    "manual": "#007AFF",  # manual edit  Apple蓝
    "none":   "#C7C7CC",  # no data  浅灰
}

def get_confidence_color(conf, method=""):
    if method == "manual":
        return CONFIDENCE_COLORS["manual"]
    if conf >= 0.8:
        return CONFIDENCE_COLORS["high"]
    elif conf >= 0.6:
        return CONFIDENCE_COLORS["medium"]
    elif conf > 0:
        return CONFIDENCE_COLORS["low"]
    return CONFIDENCE_COLORS["none"]


# ═════════════════════════════════════════════════════
#  PDF Renderer
# ═════════════════════════════════════════════════════

class PdfRenderer:
    """Renders PDF pages to PIL Images and displays them on a tkinter Canvas."""

    def __init__(self, canvas: tk.Canvas, h_scrollbar: ttk.Scrollbar, v_scrollbar: ttk.Scrollbar):
        self.canvas = canvas
        self.h_scrollbar = h_scrollbar
        self.v_scrollbar = v_scrollbar
        self.doc: fitz.Document | None = None
        self.page_count = 0
        self.current_page = 0
        self.zoom = 1.0
        self.cache: OrderedDict[int, Image.Image] = OrderedDict()  # LRU cache
        self.max_cache = 10
        self._photo_ref = None  # Prevent GC
        self._image_id = None

        # Configure scrollbars
        self.v_scrollbar.config(command=self.canvas.yview)
        self.h_scrollbar.config(command=self.canvas.xview)
        self.canvas.config(xscrollcommand=self.h_scrollbar.set, yscrollcommand=self.v_scrollbar.set)

        # Bind mouse wheel
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_mousewheel_h)

    def open_pdf(self, pdf_path: str) -> bool:
        """Open a PDF file. Returns True on success."""
        if fitz is None:
            return False
        try:
            self.doc = fitz.open(pdf_path)
            self.page_count = len(self.doc)
            self.current_page = 0
            self.cache.clear()
            return True
        except Exception as e:
            print(f"Error opening PDF: {e}")
            return False

    def close(self):
        """Close the current PDF document."""
        if self.doc:
            self.doc.close()
            self.doc = None
        self.page_count = 0
        self.current_page = 0
        self.cache.clear()
        self.canvas.delete("all")

    def go_to_page(self, page_num: int):
        """Navigate to a specific page (0-indexed)."""
        if not self.doc:
            return
        if 0 <= page_num < self.page_count:
            self.current_page = page_num
            self._render_current()

    def next_page(self):
        self.go_to_page(self.current_page + 1)

    def prev_page(self):
        self.go_to_page(self.current_page - 1)

    def set_zoom(self, zoom: float):
        """Set zoom level and re-render."""
        self.zoom = zoom
        self._render_current()

    def zoom_in(self):
        self.set_zoom(min(self.zoom * 1.25, 4.0))

    def zoom_out(self):
        self.set_zoom(max(self.zoom * 0.8, 0.25))

    def zoom_fit_width(self):
        """Zoom to fit the canvas width."""
        if not self.doc:
            return
        page = self.doc[self.current_page]
        rect = page.rect
        canvas_w = self.canvas.winfo_width()
        if canvas_w > 50:
            self.zoom = canvas_w / rect.width * 0.95
            self._render_current()

    def _render_current(self):
        """Render the current page at current zoom level."""
        if not self.doc:
            return
        page_num = self.current_page

        # Check cache
        cache_key = (page_num, self.zoom)
        if cache_key in self.cache:
            self.cache.move_to_end(cache_key)
            pil_img = self.cache[cache_key]
            self._display_image(pil_img)
            return

        # Render page
        page = self.doc[page_num]
        mat = fitz.Matrix(self.zoom, self.zoom)
        pix = page.get_pixmap(matrix=mat)
        img_data = pix.samples
        pil_img = Image.frombytes("RGB", [pix.width, pix.height], img_data)

        # Cache (LRU eviction)
        if len(self.cache) >= self.max_cache:
            self.cache.popitem(last=False)
        self.cache[cache_key] = pil_img

        self._display_image(pil_img)

    def _display_image(self, pil_img: Image.Image):
        """Display a PIL Image on the canvas."""
        self._photo_ref = ImageTk.PhotoImage(pil_img)
        self.canvas.delete("all")
        self._image_id = self.canvas.create_image(
            0, 0, anchor=tk.NW, image=self._photo_ref, tags=("page_image",)
        )
        self.canvas.config(scrollregion=(0, 0, pil_img.width, pil_img.height))

    def _on_mousewheel(self, event):
        """Vertical scroll via mouse wheel."""
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_mousewheel_h(self, event):
        """Horizontal scroll via Shift+MouseWheel."""
        self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")


# ═════════════════════════════════════════════════════
#  Bounding Box Overlay
# ═════════════════════════════════════════════════════

class BboxOverlay:
    """Draws product card bounding boxes on the PDF canvas."""

    def __init__(self, canvas: tk.Canvas):
        self.canvas = canvas
        self.selected_idx = None
        self.bbox_items = {}  # product_idx -> canvas item ids
        self._on_click_callback = None

    def set_click_callback(self, callback):
        """Set callback for bbox click: callback(product_idx)."""
        self._on_click_callback = callback
        self.canvas.tag_bind("bbox_rect", "<Button-1>", self._on_bbox_click)

    def draw_bboxes(self, products: list, current_page: int, zoom: float):
        """Draw bbox rectangles for all products on current_page."""
        self.clear()
        for idx, p in enumerate(products):
            if p.get("page") != current_page + 1:  # page is 1-indexed in products
                continue
            bbox = p.get("card_bbox", [0, 0, 100, 100])
            conf = p.get("confidence_avg", 0)

            # Scale by zoom
            x0, y0, x1, y1 = [v * zoom for v in bbox]
            color = get_confidence_color(conf)

            # Draw rectangle
            rect_id = self.canvas.create_rectangle(
                x0, y0, x1, y1,
                outline=color,
                width=2,
                tags=("bbox_rect", f"product_{idx}"),
            )
            # Label with OE number
            oe = p.get("oe_number", {}).get("value", "")[:20]
            label_id = self.canvas.create_text(
                x0 + 2, y0 - 2,
                text=oe,
                anchor=tk.SW,
                fill=color,
                font=("Microsoft YaHei", max(8, int(10 * zoom))),
                tags=("bbox_label", f"product_{idx}"),
            )
            self.bbox_items[idx] = (rect_id, label_id)

    def show_single(self, product_idx: int, products: list, current_page: int, zoom: float):
        """Draw bbox for only the specified product on the current page."""
        self.clear()
        if product_idx is None or product_idx >= len(products):
            return
        p = products[product_idx]
        if p.get("page") != current_page + 1:  # products use 1-indexed pages
            return
        bbox = p.get("card_bbox", [0, 0, 100, 100])
        conf = p.get("confidence_avg", 0)
        x0, y0, x1, y1 = [v * zoom for v in bbox]
        color = get_confidence_color(conf)
        rect_id = self.canvas.create_rectangle(
            x0, y0, x1, y1,
            outline=color, width=3,
            tags=("bbox_rect", f"product_{product_idx}"),
        )
        oe = p.get("oe_number", {}).get("value", "")[:20]
        label_id = self.canvas.create_text(
            x0 + 2, y0 - 2,
            text=oe, anchor=tk.SW, fill=color,
            font=("Microsoft YaHei", max(8, int(10 * zoom))),
            tags=("bbox_label", f"product_{product_idx}"),
        )
        self.bbox_items[product_idx] = (rect_id, label_id)
        self.selected_idx = product_idx

    def highlight_product(self, product_idx: int):
        """Highlight the bbox for a specific product."""
        if self.selected_idx is not None and self.selected_idx in self.bbox_items:
            old_items = self.bbox_items[self.selected_idx]
            for item_id in old_items:
                self.canvas.itemconfig(item_id, width=2)
        if product_idx in self.bbox_items:
            items = self.bbox_items[product_idx]
            for item_id in items:
                self.canvas.itemconfig(item_id, width=4)
                self.canvas.tag_raise(item_id)
            self.selected_idx = product_idx

    def clear(self):
        """Remove all bbox items from canvas."""
        self.canvas.delete("bbox_rect", "bbox_label")
        self.bbox_items.clear()
        self.selected_idx = None

    def _on_bbox_click(self, event):
        """Handle click on a bbox rectangle."""
        items = self.canvas.find_overlapping(event.x - 3, event.y - 3, event.x + 3, event.y + 3)
        for item_id in items:
            tags = self.canvas.gettags(item_id)
            for tag in tags:
                if tag.startswith("product_"):
                    idx = int(tag.split("_")[1])
                    if self._on_click_callback:
                        self._on_click_callback(idx)
                    return


# ═════════════════════════════════════════════════════
#  Product List Panel
# ═════════════════════════════════════════════════════

class ProductListPanel(ttk.Frame):
    """Left-center panel: Treeview of all products."""

    def __init__(self, parent):
        super().__init__(parent)
        self.on_select_callback = None

        # Search bar
        search_frame = ttk.Frame(self)
        search_frame.pack(fill=tk.X, padx=4, pady=2)
        ttk.Label(search_frame, text="搜索:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._apply_filter())
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=18)
        search_entry.pack(side=tk.LEFT, padx=2)

        ttk.Label(search_frame, text="页码:").pack(side=tk.LEFT, padx=(6, 0))
        self.page_filter_var = tk.StringVar(value="全部")
        self.page_combo = ttk.Combobox(search_frame, textvariable=self.page_filter_var,
                                       values=["全部"], width=5, state="readonly")
        self.page_combo.pack(side=tk.LEFT, padx=2)
        self.page_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())

        # Treeview
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        columns = ("confidence", "oe_number", "brand", "description_1", "page")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                 selectmode="browse")
        self.tree.heading("confidence", text="置信度")
        self.tree.heading("oe_number", text="产品编号")
        self.tree.heading("brand", text="品牌")
        self.tree.heading("description_1", text="描述一")
        self.tree.heading("page", text="页码")

        self.tree.column("confidence", width=55, anchor=tk.CENTER, stretch=False)
        self.tree.column("oe_number", width=130)
        self.tree.column("brand", width=80)
        self.tree.column("description_1", width=200)
        self.tree.column("page", width=40, anchor=tk.CENTER, stretch=False)

        # Scrollbar
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Bind selection
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # Configure row tags
        self.tree.tag_configure("high_conf", background="#E8F8ED", foreground="#1D7A3A")
        self.tree.tag_configure("med_conf", background="#FFF3E0", foreground="#C77600")
        self.tree.tag_configure("low_conf", background="#FFEBEE", foreground="#D42A20")
        self.tree.tag_configure("edited", foreground="#007AFF")

        self.all_products = []
        self.edit_state = {}
        self.row_to_idx = {}  # tree item iid -> product index

    def set_select_callback(self, callback):
        """Callback when a product row is selected: callback(product_idx)."""
        self.on_select_callback = callback

    def load_products(self, products: list, edit_state: dict = None):
        """Populate the Treeview with products."""
        self.all_products = products
        self.edit_state = edit_state if edit_state is not None else {}
        self._populate_tree(products)
        self._update_page_filter(products)

    def _populate_tree(self, products: list):
        """Insert all products into the tree."""
        self.tree.delete(*self.tree.get_children())
        self.row_to_idx.clear()

        for idx, p in enumerate(products):
            conf = p.get("confidence_avg", 0)
            oe = p.get("oe_number", {}).get("value", "")[:30]
            brand = p.get("brand", {}).get("value", "")[:20]
            d1 = p.get("description_1", {}).get("value", "")[:40]
            page = str(p.get("page", "?"))

            # Check if edited
            is_edited = idx in self.edit_state
            conf_display = f"✎ {conf:.0%}" if is_edited else f"● {conf:.0%}"

            # Determine row tag
            if is_edited:
                tag = "edited"
            elif conf >= 0.8:
                tag = "high_conf"
            elif conf >= 0.6:
                tag = "med_conf"
            else:
                tag = "low_conf"

            iid = self.tree.insert("", tk.END, values=(conf_display, oe, brand, d1, page), tags=(tag,))
            self.row_to_idx[iid] = idx

    def _apply_filter(self):
        """Filter tree rows by search text and page."""
        search_text = self.search_var.get().lower()
        page_filter = self.page_filter_var.get()

        # Show all, then hide non-matching
        for iid in self.tree.get_children():
            idx = self.row_to_idx.get(iid)
            if idx is None:
                continue
            p = self.all_products[idx]
            show = True

            # Page filter
            if page_filter != "全部":
                if str(p.get("page", "")) != page_filter:
                    show = False

            # Search filter
            if search_text:
                row_text = " ".join(str(v) for v in self.tree.item(iid, "values")).lower()
                if search_text not in row_text:
                    show = False

            if show:
                self.tree.reattach(iid, "", tk.END)
            else:
                self.tree.detach(iid)

    def _update_page_filter(self, products: list):
        """Update page filter dropdown with available pages."""
        pages = sorted(set(str(p.get("page", 1)) for p in products))
        self.page_combo["values"] = ["全部"] + pages
        self.page_combo.set("全部")

    def update_row(self, product_idx: int):
        """Update a single row after edit, merging in edit_state values."""
        if product_idx >= len(self.all_products):
            return
        p = self.all_products[product_idx]
        edits = self.edit_state.get(product_idx, {})

        # Merge edit_state into field values for display
        def _get_val(key):
            if key in edits:
                return edits[key]
            fd = p.get(key, {})
            return fd.get("value", "") if isinstance(fd, dict) else str(fd) if fd else ""

        oe = _get_val("oe_number")[:30]
        brand = _get_val("brand")[:20]
        d1 = _get_val("description_1")[:40]
        page = str(p.get("page", "?"))
        is_edited = product_idx in self.edit_state

        # Recalculate confidence with edits
        total_conf = 0.0
        field_count = 0
        for fd_def in FIELD_DEFINITIONS:
            k = fd_def["key"]
            if k in edits:
                total_conf += 1.0
                field_count += 1
            else:
                fd = p.get(k, {})
                c = fd.get("confidence", 0) if isinstance(fd, dict) else 0
                if c > 0:
                    total_conf += c
                    field_count += 1
        conf = round(total_conf / max(field_count, 1), 2)

        conf_display = f"✎ {conf:.0%}" if is_edited else f"● {conf:.0%}"
        tag = "edited" if is_edited else ("high_conf" if conf >= 0.8 else ("med_conf" if conf >= 0.6 else "low_conf"))

        for iid, idx in self.row_to_idx.items():
            if idx == product_idx:
                self.tree.item(iid, values=(conf_display, oe, brand, d1, page), tags=(tag,))
                self.tree.selection_set(iid)
                break

    def select_product(self, product_idx: int):
        """Programmatically select a product in the tree."""
        for iid, idx in self.row_to_idx.items():
            if idx == product_idx:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                break

    def get_selected_index(self) -> int | None:
        """Get the currently selected product index."""
        sel = self.tree.selection()
        if sel:
            return self.row_to_idx.get(sel[0])
        return None

    def select_next(self):
        """Select the next product in the list."""
        sel = self.tree.selection()
        if sel:
            next_iid = self.tree.next(sel[0])
            if next_iid:
                self.tree.selection_set(next_iid)
                self.tree.see(next_iid)

    def select_prev(self):
        """Select the previous product in the list."""
        sel = self.tree.selection()
        if sel:
            prev_iid = self.tree.prev(sel[0])
            if prev_iid:
                self.tree.selection_set(prev_iid)
                self.tree.see(prev_iid)

    def _on_select(self, event):
        """Handle row selection."""
        idx = self.get_selected_index()
        if idx is not None and self.on_select_callback:
            self.on_select_callback(idx)


# ═════════════════════════════════════════════════════
#  Field Editor Panel
# ═════════════════════════════════════════════════════

class FieldEditorPanel(ttk.Frame):
    """Right panel: Editable field display for a selected product."""

    def __init__(self, parent):
        super().__init__(parent)
        self.current_product_idx = None
        self.current_product = None
        self.on_save_callback = None
        self.on_navigate_callback = None
        self.on_re_select_image_callback = None

        # Product header
        self.header_label = ttk.Label(self, text="选择产品以编辑",
                                      font=("Microsoft YaHei", 11, "bold"))
        self.header_label.pack(fill=tk.X, padx=8, pady=(8, 4))

        # Divider
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=4)

        # ── Product image area (non-scrollable, always visible) ──
        self.image_frame = ttk.Frame(self, style="Panel.TFrame")
        self.image_frame.pack(fill=tk.X, padx=8, pady=4)

        # Thumbnail placeholder
        self.image_thumb_label = ttk.Label(
            self.image_frame, text="无图片",
            anchor=tk.CENTER, font=("Microsoft YaHei", 9),
        )
        self.image_thumb_label.pack(side=tk.LEFT, padx=6, pady=6)

        # Image info + button
        img_info_frame = ttk.Frame(self.image_frame)
        img_info_frame.pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=4)

        self.image_info_label = ttk.Label(
            img_info_frame, text="产品图片",
            font=("Microsoft YaHei", 9, "bold"),
        )
        self.image_info_label.pack(anchor=tk.W)

        self.image_method_label = ttk.Label(
            img_info_frame, text="",
            font=("Microsoft YaHei", 9), foreground="#86868B",
        )
        self.image_method_label.pack(anchor=tk.W)

        self.re_select_image_btn = ttk.Button(
            img_info_frame, text="📷 重新框选图片",
            command=self._on_re_select_image,
        )
        self.re_select_image_btn.pack(anchor=tk.W, pady=(4, 0))

        # Photo reference for thumbnail display
        self._thumb_photo = None
        self._thumb_size = (150, 120)  # max (w, h) for thumbnail

        # Divider after image
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=4)

        # Scrollable field area
        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        self.field_canvas = tk.Canvas(canvas_frame, highlightthickness=0,
                                       bg="#FFFFFF")
        field_scrollbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.field_canvas.yview)
        self.field_frame = ttk.Frame(self.field_canvas)

        self.field_frame.bind("<Configure>", lambda e: self.field_canvas.configure(
            scrollregion=self.field_canvas.bbox("all")))
        self._canvas_window_id = self.field_canvas.create_window((0, 0), window=self.field_frame, anchor=tk.NW)
        self.field_canvas.configure(yscrollcommand=field_scrollbar.set)

        # Adaptive sizing: resize inner frame width to fill canvas, update scrollregion
        def _on_canvas_resize(event):
            self.field_canvas.itemconfig(self._canvas_window_id, width=event.width)
            # Also constrain inner frame's requested width so Entry widgets expand properly
            self.field_frame.configure(width=event.width)
        self.field_canvas.bind("<Configure>", _on_canvas_resize, add="+")

        self.field_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        field_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse wheel for field scrolling
        self.field_canvas.bind("<MouseWheel>", lambda e: self.field_canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

        # Field widgets
        self.field_entries = {}
        self.confidence_canvases = {}
        self.confidence_labels = {}
        self.method_labels = {}
        self.re_extract_buttons = {}
        self.on_re_extract_callback = None

        for i, field_def in enumerate(FIELD_DEFINITIONS):
            self._create_field_row(field_def, i)

        # Divider
        ttk.Separator(self.field_frame, orient=tk.HORIZONTAL).grid(
            row=len(FIELD_DEFINITIONS), column=0, columnspan=4, sticky=tk.EW, pady=4)

        # Raw text display (collapsible)
        row_offset = len(FIELD_DEFINITIONS) + 1
        self.raw_text_visible = tk.BooleanVar(value=False)
        self.raw_text_toggle = ttk.Checkbutton(
            self.field_frame, text="显示原始文本", variable=self.raw_text_visible,
            command=self._toggle_raw_text)
        self.raw_text_toggle.grid(row=row_offset, column=0, columnspan=4, sticky=tk.W, padx=4, pady=2)

        self.raw_text_display = tk.Text(self.field_frame, height=4, wrap=tk.WORD,
                                         font=("Consolas", 9), state=tk.DISABLED)
        self.raw_text_display.grid(row=row_offset + 1, column=0, columnspan=4,
                                    sticky=tk.EW, padx=4, pady=2)
        self.raw_text_display.grid_remove()

        # Confidence bar
        row_offset += 2
        ttk.Label(self.field_frame, text="综合置信度:", font=("Microsoft YaHei", 9, "bold")).grid(
            row=row_offset, column=0, sticky=tk.W, padx=4, pady=2)
        self.conf_progress = ttk.Progressbar(self.field_frame, length=120, mode="determinate")
        self.conf_progress.grid(row=row_offset, column=1, columnspan=2, sticky=tk.EW, padx=2, pady=2)
        self.conf_label = ttk.Label(self.field_frame, text="0%")
        self.conf_label.grid(row=row_offset, column=3, sticky=tk.W, padx=4)

        # Action buttons — flow layout (wraps to multiple rows when narrow)
        row_offset += 1
        self.btn_container = ttk.Frame(self.field_frame)
        self.btn_container.grid(row=row_offset, column=0, columnspan=4, sticky=tk.EW, padx=4, pady=6)

        # Create button widgets and store ordered list for flow layout
        self._btn_widgets = []
        self.save_btn = ttk.Button(self.btn_container, text="💾 保存产品", command=self._on_save)
        self._btn_widgets.append(self.save_btn)
        self.reset_btn = ttk.Button(self.btn_container, text="↩ 重置", command=self._on_reset)
        self._btn_widgets.append(self.reset_btn)

        sep = ttk.Label(self.btn_container, text="")
        self._btn_widgets.append(sep)

        self.prev_btn = ttk.Button(self.btn_container, text="◀ 上条", command=lambda: self._navigate(-1))
        self._btn_widgets.append(self.prev_btn)
        self.next_btn = ttk.Button(self.btn_container, text="下条 ▶", command=lambda: self._navigate(1))
        self._btn_widgets.append(self.next_btn)

        self.btn_container.bind("<Configure>", self._reflow_buttons)

    def _create_field_row(self, field_def: dict, row: int):
        """Create a single field row with label, entry, confidence dot, and re-extract button."""
        key = field_def["key"]
        label_text = field_def["label"]

        # Label
        label = ttk.Label(self.field_frame, text=label_text + ":",
                         font=("Microsoft YaHei", 9))
        label.grid(row=row, column=0, sticky=tk.W, padx=4, pady=2)

        # Entry — click to enter PDF selection mode for re-extraction
        entry = ttk.Entry(self.field_frame, width=38, font=("Microsoft YaHei", 9))
        entry.grid(row=row, column=1, sticky=tk.EW, padx=2, pady=2)
        entry.bind("<Button-1>", lambda e, k=key: self._on_re_extract(k))
        entry.configure(cursor="crosshair")  # Visual hint: click to select on PDF
        self.field_entries[key] = entry

        # Confidence dot (tiny canvas)
        dot_canvas = tk.Canvas(self.field_frame, width=14, height=14, highlightthickness=0)
        dot_canvas.grid(row=row, column=2, sticky=tk.W, padx=2)
        dot_canvas.create_oval(2, 2, 12, 12, fill=CONFIDENCE_COLORS["none"], outline="", tags=("dot",))
        self.confidence_canvases[key] = dot_canvas

        # Re-extract button: dashed-box icon on a tiny Canvas
        re_canvas = tk.Canvas(self.field_frame, width=22, height=22,
                              highlightthickness=1, highlightbackground="#E5E5EA",
                              bg="#FFFFFF", cursor="crosshair")
        re_canvas.grid(row=row, column=3, sticky=tk.W, padx=1, pady=2)
        # Draw dashed rectangle icon
        re_canvas.create_rectangle(3, 3, 19, 19, outline="#86868B", width=2, dash=(3, 2), tags=("icon",))
        re_canvas.create_line(10, 8, 10, 14, fill="#86868B", width=1, tags=("icon",))  # crosshair hint
        re_canvas.create_line(7, 11, 13, 11, fill="#86868B", width=1, tags=("icon",))
        re_canvas.bind("<Button-1>", lambda e, k=key: self._on_re_extract(k))
        re_canvas.bind("<Enter>", lambda e, c=re_canvas: c.configure(highlightbackground="#007AFF"))
        re_canvas.bind("<Leave>", lambda e, c=re_canvas: c.configure(highlightbackground="#E5E5EA"))
        self.re_extract_buttons[key] = re_canvas

        self.field_frame.columnconfigure(1, weight=1)

    def _reflow_buttons(self, event=None):
        """Reflow action buttons: single row if they fit, wrap to 2 rows if not."""
        if not hasattr(self, '_btn_widgets') or not self._btn_widgets:
            return
        container_w = self.btn_container.winfo_width()
        if container_w < 10:
            return

        pad_x, pad_y = 3, 3
        gap_x = 6  # horizontal gap between button groups (separator)

        # Measure each widget's natural width
        widths = []
        heights = []
        for w in self._btn_widgets:
            w.update_idletasks()
            widths.append(w.winfo_reqwidth())
            heights.append(w.winfo_reqheight())
        row_h = max(heights) if heights else 30

        # Try single row: save_btn + reset_btn + gap + prev_btn + next_btn
        # Group 1: save + reset (indices 0-1), separator at 2, Group 2: prev + next (indices 3-4)
        g1_w = widths[0] + pad_x + widths[1]  # save + reset
        g2_w = widths[3] + pad_x + widths[4]  # prev + next
        single_row_w = g1_w + gap_x + g2_w + pad_x * 2

        if single_row_w <= container_w:
            # All fit in one row
            x = pad_x
            y = pad_y
            for i, w in enumerate(self._btn_widgets):
                if i == 2:  # separator
                    x += gap_x
                    continue
                w.place(x=x, y=y, width=widths[i], height=row_h)
                x += widths[i] + pad_x
            total_h = row_h + pad_y * 2
        else:
            # Two rows: save+reset on row 0, prev+next on row 1
            g1_total = g1_w + pad_x
            g2_total = g2_w + pad_x
            # Row 0: save + reset
            x = pad_x
            y = pad_y
            for i in (0, 1):
                self._btn_widgets[i].place(x=x, y=y, width=widths[i], height=row_h)
                x += widths[i] + pad_x
            # Hide separator
            self._btn_widgets[2].place_forget()
            # Row 1: prev + next
            x = pad_x
            y = row_h + pad_y * 2
            for i in (3, 4):
                self._btn_widgets[i].place(x=x, y=y, width=widths[i], height=row_h)
                x += widths[i] + pad_x
            total_h = row_h * 2 + pad_y * 3

        # Update container height so scrollregion accounts for it
        self.btn_container.configure(height=total_h)
        # Refresh canvas scrollregion after reflow
        self.field_frame.update_idletasks()
        self.field_canvas.configure(scrollregion=self.field_canvas.bbox("all"))

    def rebuild_fields(self):
        """Destroy and recreate all field rows from FIELD_DEFINITIONS (after config change)."""
        # Destroy existing widgets in field_frame
        for widget in self.field_frame.winfo_children():
            widget.destroy()

        # Clear references
        self.field_entries.clear()
        self.confidence_canvases.clear()
        self.confidence_labels.clear()
        self.method_labels.clear()
        self.re_extract_buttons.clear()

        # Recreate field rows
        for i, field_def in enumerate(FIELD_DEFINITIONS):
            self._create_field_row(field_def, i)

        # Re-add separator, raw text toggle, confidence bar, action buttons
        row_offset = len(FIELD_DEFINITIONS)

        ttk.Separator(self.field_frame, orient=tk.HORIZONTAL).grid(
            row=row_offset, column=0, columnspan=4, sticky=tk.EW, pady=4)

        row_offset += 1
        self.raw_text_visible = tk.BooleanVar(value=False)
        self.raw_text_toggle = ttk.Checkbutton(
            self.field_frame, text="显示原始文本", variable=self.raw_text_visible,
            command=self._toggle_raw_text)
        self.raw_text_toggle.grid(row=row_offset, column=0, columnspan=4, sticky=tk.W, padx=4, pady=2)

        self.raw_text_display = tk.Text(self.field_frame, height=4, wrap=tk.WORD,
                                         font=("Consolas", 9), state=tk.DISABLED,
                                         bg="#F9F9FC", fg="#1D1D1F", insertbackground="#1D1D1F",
                                         relief="solid", borderwidth=1,
                                         highlightthickness=0, padx=4, pady=4)
        self.raw_text_display.grid(row=row_offset + 1, column=0, columnspan=4,
                                    sticky=tk.EW, padx=4, pady=2)
        self.raw_text_display.grid_remove()

        row_offset += 2
        ttk.Label(self.field_frame, text="综合置信度:", font=("Microsoft YaHei", 9, "bold")).grid(
            row=row_offset, column=0, sticky=tk.W, padx=4, pady=2)
        self.conf_progress = ttk.Progressbar(self.field_frame, length=120, mode="determinate")
        self.conf_progress.grid(row=row_offset, column=1, columnspan=2, sticky=tk.EW, padx=2, pady=2)
        self.conf_label = ttk.Label(self.field_frame, text="0%")
        self.conf_label.grid(row=row_offset, column=3, sticky=tk.W, padx=4)

        row_offset += 1
        self.btn_container = ttk.Frame(self.field_frame)
        self.btn_container.grid(row=row_offset, column=0, columnspan=4, sticky=tk.EW, padx=4, pady=6)

        self._btn_widgets = []
        self.save_btn = ttk.Button(self.btn_container, text="💾 保存产品", command=self._on_save)
        self._btn_widgets.append(self.save_btn)
        self.reset_btn = ttk.Button(self.btn_container, text="↩ 重置", command=self._on_reset)
        self._btn_widgets.append(self.reset_btn)

        sep = ttk.Label(self.btn_container, text="")
        self._btn_widgets.append(sep)

        self.prev_btn = ttk.Button(self.btn_container, text="◀ 上条", command=lambda: self._navigate(-1))
        self._btn_widgets.append(self.prev_btn)
        self.next_btn = ttk.Button(self.btn_container, text="下条 ▶", command=lambda: self._navigate(1))
        self._btn_widgets.append(self.next_btn)

        self.btn_container.bind("<Configure>", self._reflow_buttons)

        # Refresh scroll region after widget recreation
        self.field_frame.update_idletasks()
        # Trigger initial button layout
        self._reflow_buttons()
        self.field_canvas.configure(scrollregion=self.field_canvas.bbox("all"))
        # Re-apply canvas window width so Entry widgets fill available space
        cw = self.field_canvas.winfo_width()
        if cw > 1:
            self.field_canvas.itemconfig(self._canvas_window_id, width=cw)

    def set_callbacks(self, on_save=None, on_navigate=None, on_re_extract=None, on_re_select_image=None):
        """Set callbacks for save, navigation, re-extract, and image re-select actions."""
        self.on_save_callback = on_save
        self.on_navigate_callback = on_navigate
        self.on_re_extract_callback = on_re_extract
        self.on_re_select_image_callback = on_re_select_image

    def populate(self, product: dict, product_idx: int, edit_state: dict):
        """Fill the field editor with product data."""
        self.current_product_idx = product_idx
        self.current_product = product

        # Header
        page = product.get("page", "?")
        card = product.get("card_index", "?")
        oe = product.get("oe_number", {}).get("value", "N/A")
        self.header_label.config(text=f"产品 #{product_idx+1} | 第{page}页 卡片{card} | {oe}")

        # Fields
        for field_def in FIELD_DEFINITIONS:
            key = field_def["key"]
            field_data = product.get(key, {})
            if not isinstance(field_data, dict):
                field_data = {"value": str(field_data) if field_data else "", "confidence": 0.0, "method": "none"}

            value = field_data.get("value", "")
            conf = field_data.get("confidence", 0.0)
            method = field_data.get("method", "none")

            # Check for manual edit
            if product_idx in edit_state and key in edit_state[product_idx]:
                value = edit_state[product_idx][key]
                conf = 1.0
                method = "manual"

            # Update entry
            entry = self.field_entries.get(key)
            if entry:
                entry.delete(0, tk.END)
                entry.insert(0, value)

            # Update confidence dot
            color = get_confidence_color(conf, method)
            dot = self.confidence_canvases.get(key)
            if dot:
                dot.itemconfig("dot", fill=color)

        # Raw text
        self.raw_text_display.config(state=tk.NORMAL)
        self.raw_text_display.delete("1.0", tk.END)
        self.raw_text_display.insert("1.0", product.get("raw_text", ""))
        self.raw_text_display.config(state=tk.DISABLED)

        # Confidence bar
        conf_avg = product.get("confidence_avg", 0)
        self.conf_progress["value"] = conf_avg * 100
        self.conf_label.config(text=f"{conf_avg:.0%}")

        # Product image
        self._update_image_display(product, product_idx, edit_state)

        # Enable buttons
        self.save_btn.config(state=tk.NORMAL)
        self.reset_btn.config(state=tk.NORMAL)

    def clear(self):
        """Clear all fields."""
        self.current_product_idx = None
        self.current_product = None
        self.header_label.config(text="选择产品以编辑")
        for entry in self.field_entries.values():
            entry.delete(0, tk.END)
        for dot in self.confidence_canvases.values():
            dot.itemconfig("dot", fill=CONFIDENCE_COLORS["none"])
        self.conf_progress["value"] = 0
        self.conf_label.config(text="0%")
        self.save_btn.config(state=tk.DISABLED)
        self.reset_btn.config(state=tk.DISABLED)
        self._clear_image_display()

    def get_edited_values(self) -> dict:
        """Get all current field values from entry widgets."""
        values = {}
        for field_def in FIELD_DEFINITIONS:
            key = field_def["key"]
            entry = self.field_entries.get(key)
            if entry:
                values[key] = entry.get().strip()
        return values

    def _on_save(self):
        """Handle save button click."""
        if self.on_save_callback and self.current_product_idx is not None:
            values = self.get_edited_values()
            self.on_save_callback(self.current_product_idx, values)

    def _on_reset(self):
        """Reset fields to original values."""
        if self.current_product is not None and self.current_product_idx is not None:
            self.populate(self.current_product, self.current_product_idx, {})

    def _navigate(self, direction: int):
        """Navigate to next/previous product."""
        if self.on_navigate_callback:
            self.on_navigate_callback(direction)

    def _on_re_extract(self, field_key: str):
        """Handle re-extract button click - enter PDF selection mode."""
        if self.on_re_extract_callback:
            self.on_re_extract_callback(field_key)

    # ─── Product Image Display ───

    def set_images_dir(self, images_dir: str):
        """Set the directory where product images are stored."""
        self._images_dir = images_dir

    def _update_image_display(self, product: dict, product_idx: int, edit_state: dict):
        """Show the product's matched image as a thumbnail."""
        # Check for manually selected image in edit_state
        manual_image = None
        if product_idx in edit_state and "_product_image" in edit_state[product_idx]:
            manual_image = edit_state[product_idx]["_product_image"]

        # Find matched image file
        image_path = None
        method_text = ""
        conf_text = ""

        if manual_image and os.path.isfile(manual_image):
            image_path = manual_image
            method_text = "手动框选"
            conf_text = ""
        else:
            matched_images = product.get("_matched_images", [])
            if matched_images:
                mi = matched_images[0]  # Show first matched image
                filename = mi.get("filename", "")
                method = mi.get("method", "")
                conf = mi.get("confidence", 0)

                img_dir = getattr(self, "_images_dir", "")
                if img_dir and filename:
                    candidate = os.path.join(img_dir, filename)
                    if os.path.isfile(candidate):
                        image_path = candidate
                if not image_path and img_dir and filename:
                    # Try in subfolder
                    pdf_name = os.path.basename(os.path.dirname(img_dir))
                    candidate = os.path.join(img_dir, pdf_name + "_images", filename)
                    if os.path.isfile(candidate):
                        image_path = candidate

                method_map = {
                    "containment": "空间包含",
                    "nearest_distance": "最近距离",
                    "vertical_alignment": "垂直对齐",
                    "reading_order": "阅读顺序",
                }
                method_text = method_map.get(method, method)
                conf_text = f" • {conf:.0%}"

        if image_path:
            try:
                pil_img = Image.open(image_path)
                # Calculate thumbnail size preserving aspect ratio
                max_w, max_h = self._thumb_size
                pil_img.thumbnail((max_w, max_h), Image.LANCZOS)
                self._thumb_photo = ImageTk.PhotoImage(pil_img)
                self.image_thumb_label.configure(
                    image=self._thumb_photo, text="",
                    relief=tk.FLAT, borderwidth=0,
                )
                self.image_info_label.config(text="产品图片")
                self.image_method_label.config(
                    text=f"{method_text}{conf_text}"
                )
            except Exception as e:
                self._clear_image_display()
                self.image_info_label.config(text=f"图片加载失败: {e}")
        else:
            self._clear_image_display()
            if matched_images := product.get("_matched_images", []):
                filename = matched_images[0].get("filename", "")
                self.image_info_label.config(text="产品图片 (文件未找到)")
                self.image_method_label.config(text=filename)
            else:
                self.image_info_label.config(text="产品图片 (未关联)")
                self.image_method_label.config(text="")

    def _clear_image_display(self):
        """Reset image area to placeholder."""
        self._thumb_photo = None
        self.image_thumb_label.configure(
            image="", text="无图片",
            relief=tk.GROOVE, borderwidth=1,
        )

    def _on_re_select_image(self):
        """Handle re-select image button click."""
        if self.on_re_select_image_callback and self.current_product_idx is not None:
            self.on_re_select_image_callback(self.current_product_idx)

    def highlight_re_extract_button(self, field_key: str, active: bool):
        """Highlight/unhighlight the re-extract canvas button for a field."""
        canvas = self.re_extract_buttons.get(field_key)
        if canvas and isinstance(canvas, tk.Canvas):
            if active:
                canvas.configure(highlightbackground="#FF9800", highlightthickness=2)
            else:
                canvas.configure(highlightbackground="#BDBDBD", highlightthickness=1)

    def _toggle_raw_text(self):
        """Toggle raw text display visibility."""
        if self.raw_text_visible.get():
            self.raw_text_display.grid()
        else:
            self.raw_text_display.grid_remove()


# ═════════════════════════════════════════════════════
#  Pipeline Runner
# ═════════════════════════════════════════════════════

class PipelineRunner:
    """Runs the extraction pipeline in a background thread."""

    def __init__(self):
        self.queue = queue.Queue()
        self.thread = None
        self.cancelled = False

    def run(self, pdf_path: str, output_dir: str, ocr_engine="paddleocr"):
        """Start pipeline in background thread."""
        self.cancelled = False
        self.thread = threading.Thread(
            target=self._run_pipeline,
            args=(pdf_path, output_dir, ocr_engine),
            daemon=True,
        )
        self.thread.start()

    def cancel(self):
        """Cancel the running pipeline."""
        self.cancelled = True

    def is_running(self) -> bool:
        """Check if pipeline is still running."""
        return self.thread is not None and self.thread.is_alive()

    def _run_pipeline(self, pdf_path: str, output_dir: str, ocr_engine: str):
        """Internal: run the step5 pipeline."""
        try:
            pdf_name = Path(pdf_path).stem
            self.queue.put(("progress", "Step 1/4: PDF解析 + OCR..."))

            from step1_pdf_parse import parse_pdf, export_images

            # Initialize OCR
            ocr = None
            if ocr_engine == "paddleocr":
                from ocr_engine import OCREngine
                try:
                    ocr = OCREngine(engine="paddleocr")
                except Exception as e:
                    self.queue.put(("warning", f"OCR初始化失败: {e}"))

            parsed = parse_pdf(pdf_path, ocr)

            if self.cancelled:
                return

            # Export images
            step_output = os.path.join(output_dir, pdf_name)
            os.makedirs(step_output, exist_ok=True)
            images_dir = os.path.join(step_output, f"{pdf_name}_images")
            export_images(parsed.get("doc"), parsed.get("pages", []), step_output, pdf_name)

            if self.cancelled:
                return

            # Step 2+3: Field extraction
            self.queue.put(("progress", "Step 2/4: 字段抽取..."))
            extractor = FieldExtractor()
            all_products = []

            for i, page_data in enumerate(parsed.get("pages", [])):
                if self.cancelled:
                    return
                if page_data.get("is_scanned") and not page_data.get("ocr_applied"):
                    text_blocks = page_data.get("text_blocks", [])
                else:
                    text_blocks = page_data.get("text_blocks", [])

                if not text_blocks:
                    continue

                page_products = extractor.extract_from_page({
                    "text_blocks": text_blocks,
                    "images": page_data.get("images", []),
                    "card_regions": page_data.get("card_regions", []),
                    "page_size": page_data.get("page_size", [0, 0, 600, 800]),
                }, page_data.get("page_num", i + 1))

                all_products.extend(page_products)

            if self.cancelled:
                return

            # Step 4: Image matching (per-page)
            self.queue.put(("progress", "Step 3/4: 图片关联..."))
            all_matches = {}
            try:
                for page_data in parsed.get("pages", []):
                    page_num = page_data.get("page_num", 1)
                    page_imgs = page_data.get("images", [])
                    page_size = page_data.get("page_size", (600, 800))
                    if page_imgs:
                        matches = match_images_to_products(all_products, page_imgs, page_size, page_num)
                        if matches:
                            all_matches[str(page_num)] = matches
            except Exception as e:
                self.queue.put(("warning", f"图片关联失败: {e}"))

            # ── Augment products with matched image info ──
            for page_matches in all_matches.values():
                for match in page_matches:
                    global_idx = match.get("product_idx")
                    if global_idx is not None and global_idx < len(all_products):
                        product = all_products[global_idx]
                        if "_matched_images" not in product:
                            product["_matched_images"] = []
                        xref = match.get("image_xref", match.get("image_idx", 0))
                        ext = match.get("image_ext", "jpeg")
                        filename = f"p{product.get('page', 0):03d}_xref{xref}.{ext}"
                        product["_matched_images"].append({
                            "xref": xref,
                            "filename": filename,
                            "bbox": match.get("image_bbox", []),
                            "confidence": match.get("confidence", 0),
                            "method": match.get("method", ""),
                        })

            if self.cancelled:
                return

            # Export
            self.queue.put(("progress", "Step 4/4: 导出..."))
            fields_path = os.path.join(step_output, f"{pdf_name}_fields.json")

            # Build output structure
            output_data = {
                "source_file": pdf_name,
                "total_products": len(all_products),
                "products": all_products,
            }
            with open(fields_path, "w", encoding="utf-8") as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)

            # Excel export
            excel_path = os.path.join(step_output, f"{pdf_name}_output.xlsx")
            try:
                step5_export_to_excel(all_products, all_matches, images_dir, excel_path, image_mode="embed")
            except Exception as e:
                self.queue.put(("warning", f"Excel导出失败: {e}"))

            # Report
            report = {
                "total_pages": len(parsed.get("pages", [])),
                "total_products": len(all_products),
                "total_images": sum(len(p.get("images", [])) for p in parsed.get("pages", [])),
                "matched_images": sum(len(v) for v in all_matches.values()),
                "avg_confidence": round(
                    sum(p.get("confidence_avg", 0) for p in all_products) / max(len(all_products), 1), 2
                ) if all_products else 0,
            }

            self.queue.put(("done", all_products, report, fields_path, excel_path, images_dir, all_matches))

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.queue.put(("error", str(e)))

    def load_from_json(self, json_path: str) -> tuple:
        """Load products directly from a JSON file (skip pipeline)."""
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        products = data.get("products", [])
        report = {
            "total_pages": len(set(p.get("page", 1) for p in products)),
            "total_products": len(products),
            "total_images": sum(len(p.get("_matched_images", [])) for p in products),
            "avg_confidence": round(
                sum(p.get("confidence_avg", 0) for p in products) / max(len(products), 1), 2
            ) if products else 0,
        }
        # Find images directory
        json_dir = os.path.dirname(json_path)
        pdf_name = data.get("source_file", "")
        images_dir = os.path.join(json_dir, f"{pdf_name}_images")
        if not os.path.isdir(images_dir):
            images_dir = os.path.join(json_dir, pdf_name, f"{pdf_name}_images")

        # Reconstruct all_matches from products' _matched_images
        all_matches = {}
        for global_idx, p in enumerate(products):
            matched_imgs = p.get("_matched_images", [])
            if not matched_imgs:
                continue
            page = str(p.get("page", 1))
            if page not in all_matches:
                all_matches[page] = []
            for mi in matched_imgs:
                all_matches[page].append({
                    "product_idx": global_idx,
                    "image_idx": mi.get("xref", 0),
                    "image_xref": mi.get("xref", 0),
                    "image_bbox": mi.get("bbox", []),
                    "image_ext": mi.get("filename", "").rsplit(".", 1)[-1] if mi.get("filename") else "jpeg",
                    "confidence": mi.get("confidence", 0),
                    "method": mi.get("method", ""),
                })
        return products, report, json_path, images_dir, all_matches


# ═════════════════════════════════════════════════════
#  Main Application
# ═════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════
#  Field Configuration Dialog
# ═════════════════════════════════════════════════════

class FieldConfigDialog(tk.Toplevel):
    """Modal dialog for editing field definitions with template support."""

    def __init__(self, parent, current_defs: list, templates: dict = None, active_name: str = ""):
        super().__init__(parent)
        self.title("全局字段配置")
        self.geometry("620x540")
        self.resizable(True, True)
        self.minsize(520, 400)
        self.transient(parent)
        self.grab_set()

        # Template state
        self.templates = dict(templates) if templates else {}
        if not self.templates:
            self.templates = {"默认模板": list(DEFAULT_FIELD_DEFINITIONS)}
        self.active_name = active_name if active_name in self.templates else list(self.templates.keys())[0]
        # Ensure current defs are saved to active template before editing
        self.templates[self.active_name] = [dict(d) for d in current_defs]

        self.result = None  # Will hold (definitions, templates, active_name) on save

        # Working copy
        self.defs = [dict(d) for d in self.templates[self.active_name]]

        self._build_ui()
        self._populate_list()

        # Center on parent
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0,x)}+{max(0,y)}")

        self.wait_window()

    def _build_ui(self):
        """Build the dialog layout with template selector."""
        # ── Top: Template selector ──
        template_frame = ttk.Frame(self)
        template_frame.pack(fill=tk.X, padx=8, pady=(8, 0))

        ttk.Label(template_frame, text="当前模板:",
                  font=("Microsoft YaHei", 9, "bold")).pack(side=tk.LEFT)

        self.template_var = tk.StringVar(value=self.active_name)
        self.template_combo = ttk.Combobox(
            template_frame, textvariable=self.template_var,
            values=list(self.templates.keys()), state="readonly", width=16,
        )
        self.template_combo.pack(side=tk.LEFT, padx=4)
        self.template_combo.bind("<<ComboboxSelected>>", self._on_template_select)

        ttk.Button(template_frame, text="另存为...",
                   command=self._save_as_template, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Button(template_frame, text="删除",
                   command=self._delete_template, width=6).pack(side=tk.LEFT, padx=2)

        ttk.Label(template_frame, text="切换模板会丢弃未保存的修改",
                  font=("Microsoft YaHei", 8), foreground="#86868B").pack(side=tk.RIGHT)

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=8, pady=6)

        # ── Main: listbox (left) + detail editor (right) ──
        main_frame = ttk.Frame(self)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=0)

        # Left: field list
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Label(left_frame, text="字段列表:", font=("Microsoft YaHei", 9, "bold")).pack(anchor=tk.W)

        list_frame = ttk.Frame(left_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=2)

        self.listbox = tk.Listbox(list_frame, font=("Microsoft YaHei", 9),
                                   selectmode=tk.SINGLE, exportselection=False,
                                   bg="#FFFFFF", fg="#1D1D1F", selectbackground="#007AFF",
                                   selectforeground="#FFFFFF", relief="solid",
                                   highlightthickness=1, highlightbackground="#E5E5EA")
        list_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=list_scroll.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        # List manipulation buttons
        btn_row = ttk.Frame(left_frame)
        btn_row.pack(fill=tk.X, pady=2)
        ttk.Button(btn_row, text="＋ 添加", command=self._add_field).pack(side=tk.LEFT, padx=1)
        ttk.Button(btn_row, text="－ 删除", command=self._remove_field).pack(side=tk.LEFT, padx=1)
        ttk.Button(btn_row, text="▲", width=3, command=self._move_up).pack(side=tk.LEFT, padx=1)
        ttk.Button(btn_row, text="▼", width=3, command=self._move_down).pack(side=tk.LEFT, padx=1)

        # Right: detail editor
        right_frame = ttk.LabelFrame(main_frame, text="字段详情", padding=8)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(8, 0))

        ttk.Label(right_frame, text="字段Key:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(right_frame, textvariable=self.key_var, width=24, font=("Consolas", 9))
        self.key_entry.grid(row=0, column=1, sticky=tk.EW, pady=2, padx=2)
        self.key_entry.bind("<KeyRelease>", lambda e: self._on_detail_change())

        ttk.Label(right_frame, text="显示标签:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.label_var = tk.StringVar()
        self.label_entry = ttk.Entry(right_frame, textvariable=self.label_var, width=24,
                                      font=("Microsoft YaHei", 9))
        self.label_entry.grid(row=1, column=1, sticky=tk.EW, pady=2, padx=2)
        self.label_entry.bind("<KeyRelease>", lambda e: self._on_detail_change())

        self.editable_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right_frame, text="可编辑", variable=self.editable_var,
                        command=self._on_detail_change).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=4)

        ttk.Label(right_frame, text="修改Key或标签后自动更新列表", font=("Microsoft YaHei", 9),
                  foreground="#86868B").grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=4)

        right_frame.columnconfigure(1, weight=1)

        # ── Bottom: action buttons ──
        bottom_frame = ttk.Frame(self)
        bottom_frame.pack(fill=tk.X, padx=8, pady=(4, 8))

        # Hint / status feedback
        self.status_hint = ttk.Label(bottom_frame, text="Key不可重复",
                                     font=("Microsoft YaHei", 9), foreground="#86868B")
        self.status_hint.pack(side=tk.LEFT)

        ttk.Button(bottom_frame, text="恢复默认", command=self._reset_defaults,
                   width=9).pack(side=tk.LEFT, padx=4)

        # Right-aligned action buttons (ordered right-to-left in pack)
        ttk.Button(bottom_frame, text="取消", command=self.destroy,
                   width=8).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bottom_frame, text="确定", command=self._on_confirm,
                   width=8).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bottom_frame, text="应用", command=self._on_apply,
                   width=8).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bottom_frame, text="保存模板", command=self._on_save_template,
                   width=10).pack(side=tk.RIGHT, padx=2)

    def _populate_list(self):
        """Fill the listbox from self.defs."""
        self.listbox.delete(0, tk.END)
        for d in self.defs:
            self.listbox.insert(tk.END, f"{d['key']}  →  {d['label']}")
        if self.defs:
            self.listbox.selection_set(0)
            self._on_list_select()

    def _on_list_select(self, event=None):
        """Populate detail editor from selected list item."""
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        d = self.defs[idx]
        self._suppress_change = True
        self.key_var.set(d.get("key", ""))
        self.label_var.set(d.get("label", ""))
        self.editable_var.set(d.get("editable", True))
        self._suppress_change = False
        self._selected_idx = idx

    _suppress_change = False
    _selected_idx = -1

    def _on_detail_change(self):
        """Update self.defs when detail fields change."""
        if self._suppress_change:
            return
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.defs[idx]["key"] = self.key_var.get()
        self.defs[idx]["label"] = self.label_var.get()
        self.defs[idx]["editable"] = self.editable_var.get()
        # Update listbox entry
        self.listbox.delete(idx)
        self.listbox.insert(idx, f"{self.defs[idx]['key']}  →  {self.defs[idx]['label']}")
        self.listbox.selection_set(idx)

    def _add_field(self):
        """Add a new field definition."""
        new_key = f"field_{len(self.defs)+1}"
        new_def = {"key": new_key, "label": "新字段", "editable": True}
        self.defs.append(new_def)
        self.listbox.insert(tk.END, f"{new_key}  →  新字段")
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(len(self.defs) - 1)
        self.listbox.see(len(self.defs) - 1)
        self._on_list_select()

    def _remove_field(self):
        """Remove selected field definition."""
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if len(self.defs) <= 1:
            messagebox.showwarning("无法删除", "至少需要保留一个字段", parent=self)
            return
        del self.defs[idx]
        self.listbox.delete(idx)
        if idx >= len(self.defs):
            idx = len(self.defs) - 1
        if self.defs:
            self.listbox.selection_set(idx)
            self._on_list_select()

    def _move_up(self):
        """Move selected field up."""
        sel = self.listbox.curselection()
        if not sel or sel[0] == 0:
            return
        idx = sel[0]
        self.defs[idx], self.defs[idx - 1] = self.defs[idx - 1], self.defs[idx]
        self._populate_list()
        self.listbox.selection_set(idx - 1)
        self._on_list_select()

    def _move_down(self):
        """Move selected field down."""
        sel = self.listbox.curselection()
        if not sel or sel[0] >= len(self.defs) - 1:
            return
        idx = sel[0]
        self.defs[idx], self.defs[idx + 1] = self.defs[idx + 1], self.defs[idx]
        self._populate_list()
        self.listbox.selection_set(idx + 1)
        self._on_list_select()

    def _reset_defaults(self):
        """Reset to default field definitions."""
        if messagebox.askyesno("恢复默认", "确定要恢复为默认字段配置吗？\n当前修改将丢失。", parent=self):
            self.defs = [dict(d) for d in DEFAULT_FIELD_DEFINITIONS]
            self._populate_list()

    def _validate_keys(self) -> bool:
        """Check for duplicate or empty keys."""
        keys = [d.get("key", "").strip() for d in self.defs]
        for i, k in enumerate(keys):
            if not k:
                messagebox.showwarning("字段配置错误", f"第 {i+1} 个字段的Key不能为空", parent=self)
                return False
            if k in keys[:i]:
                messagebox.showwarning("字段配置错误", f"字段Key '{k}' 重复", parent=self)
                return False
        return True

    def _on_save_template(self):
        """Save current definitions to the active template (stay in dialog)."""
        if not self._validate_keys():
            return
        self.templates[self.active_name] = [dict(d) for d in self.defs]
        if save_templates(self.templates, self.active_name):
            self.status_hint.config(text=f"模板「{self.active_name}」已保存 ({len(self.defs)} 字段)")
        else:
            messagebox.showerror("保存失败", "无法写入模板文件", parent=self)

    def _on_apply(self):
        """Save template, apply to main app, and close dialog."""
        if not self._validate_keys():
            return
        self.templates[self.active_name] = [dict(d) for d in self.defs]
        save_templates(self.templates, self.active_name)
        self.result = (self.templates[self.active_name], dict(self.templates), self.active_name)
        self.destroy()

    def _on_confirm(self):
        """Same as apply — save, apply, close."""
        self._on_apply()

    def _on_template_select(self, event=None):
        """Switch to a different template."""
        new_name = self.template_var.get()
        if new_name == self.active_name:
            return
        # Confirm discard unsaved changes
        if new_name in self.templates:
            if not messagebox.askyesno(
                "切换模板",
                f"切换到模板「{new_name}」将丢弃当前未保存的修改，\n是否继续？",
                parent=self,
            ):
                self.template_var.set(self.active_name)
                return
            self.active_name = new_name
            self.defs = [dict(d) for d in self.templates[self.active_name]]
            self._populate_list()

    def _save_as_template(self):
        """Save current field definitions as a new template."""
        if not self._validate_keys():
            return

        # Prompt for template name
        dialog = tk.Toplevel(self)
        dialog.title("另存为模板")
        dialog.geometry("320x120")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(dialog, text="模板名称:",
                  font=("Microsoft YaHei", 10)).pack(padx=12, pady=(12, 4), anchor=tk.W)
        name_var = tk.StringVar()
        name_entry = ttk.Entry(dialog, textvariable=name_var, width=30,
                               font=("Microsoft YaHei", 10))
        name_entry.pack(padx=12, fill=tk.X)
        name_entry.focus_set()

        result = [None]

        def on_ok():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("名称错误", "模板名称不能为空", parent=dialog)
                return
            if name in self.templates:
                if not messagebox.askyesno(
                    "覆盖确认",
                    f"模板「{name}」已存在，是否覆盖？",
                    parent=dialog,
                ):
                    return
            result[0] = name
            dialog.destroy()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=tk.X, padx=12, pady=12)
        ttk.Button(btn_frame, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=2)
        ttk.Button(btn_frame, text="保存", command=on_ok).pack(side=tk.RIGHT, padx=2)

        dialog.bind("<Return>", lambda e: on_ok())
        dialog.bind("<Escape>", lambda e: dialog.destroy())

        # Center
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(0,x)}+{max(0,y)}")

        self.wait_window(dialog)

        if result[0]:
            self.templates[result[0]] = [dict(d) for d in self.defs]
            self.active_name = result[0]
            self.template_combo["values"] = list(self.templates.keys())
            self.template_var.set(self.active_name)
            messagebox.showinfo("保存成功", f"模板「{self.active_name}」已保存", parent=self)

    def _delete_template(self):
        """Delete the current template."""
        if len(self.templates) <= 1:
            messagebox.showwarning("无法删除", "至少需要保留一个模板", parent=self)
            return
        if not messagebox.askyesno(
            "删除模板",
            f"确定要删除模板「{self.active_name}」吗？\n"
            f"（当前字段配置将丢失，请先另存为其他模板）",
            parent=self,
        ):
            return
        del self.templates[self.active_name]
        self.active_name = list(self.templates.keys())[0]
        self.defs = [dict(d) for d in self.templates[self.active_name]]
        self.template_combo["values"] = list(self.templates.keys())
        self.template_var.set(self.active_name)
        self._populate_list()


# ═════════════════════════════════════════════════════
#  Batch Apply Dialog
# ═════════════════════════════════════════════════════

class BatchApplyDialog(tk.Toplevel):
    """Dialog to batch-apply an OCR field value to multiple products on the same page."""

    def __init__(self, parent, field_key: str, field_label: str, text: str,
                 products: list, current_idx: int, edit_state: dict,
                 on_apply_callback):
        super().__init__(parent)
        self.title(f"批量应用 — {field_label}")
        self.geometry("520x550")
        self.resizable(True, True)
        self.minsize(400, 300)
        self.transient(parent)
        self.grab_set()

        self.field_key = field_key
        self.field_label = field_label
        self.text = text
        self.products = products  # list of (idx, oe, brand, desc1, page)
        self.current_idx = current_idx
        self.edit_state = edit_state
        self.on_apply_callback = on_apply_callback

        # Checkbox variables
        self.check_vars = {}  # product_idx -> tk.BooleanVar

        self._build_ui()
        self._populate_list()

        # Center on parent
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0,x)}+{max(0,y)}")

        self.wait_window()

    def _build_ui(self):
        """Build dialog layout with bottom buttons always visible."""

        # ── Bottom section: pack first (anchored to bottom) ──
        # Action buttons (bottom-most)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(4, 12))
        self.skip_btn = ttk.Button(btn_frame, text="仅当前产品 (跳过) [回车]", command=self._skip)
        self.skip_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=2)
        self.apply_btn = ttk.Button(btn_frame, text="✓ 确认应用", command=self._on_apply)
        self.apply_btn.pack(side=tk.RIGHT, padx=4)

        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=4)

        # Select all / none
        sel_frame = ttk.Frame(self)
        sel_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=4)
        ttk.Button(sel_frame, text="全选", command=self._select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(sel_frame, text="全不选", command=self._select_none).pack(side=tk.LEFT, padx=2)
        ttk.Label(sel_frame, text=f"共 {len(self.products)} 个同页产品",
                  font=("Microsoft YaHei", 9), foreground="#86868B").pack(side=tk.RIGHT)

        # ── Middle section: scrollable product list (fills remaining space) ──
        list_container = ttk.Frame(self)
        list_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        canvas = tk.Canvas(list_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=canvas.yview)
        self.check_frame = ttk.Frame(canvas)

        self.check_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.check_frame, anchor=tk.NW)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse wheel for scrolling
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # ── Top section: header + instruction ──
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=8, pady=4)

        # Instruction
        instr = ttk.Frame(self)
        instr.pack(fill=tk.X, padx=12, pady=2)
        ttk.Label(instr, text="勾选需要批量更新此字段的产品 (同页产品):",
                  font=("Microsoft YaHei", 9)).pack(anchor=tk.W)

        # Header info
        header = ttk.Frame(self)
        header.pack(fill=tk.X, padx=12, pady=(12, 4))

        ttk.Label(header, text=f"字段: {self.field_label}", font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W)
        ttk.Label(header, text=f"OCR结果: \"{self.text[:60]}{'...' if len(self.text)>60 else ''}\"",
                  font=("Microsoft YaHei", 9), foreground="#007AFF").pack(anchor=tk.W, pady=2)

        # Keyboard shortcuts: Space / Enter → skip (quick dismiss)
        self.bind("<Return>", lambda e: self._skip())
        self.bind("<space>", lambda e: self._skip())
        self.bind("<Escape>", lambda e: self._skip())

        # Default focus on skip button
        self.skip_btn.focus_set()

    def _populate_list(self):
        """Populate the checklist with products."""
        for idx, oe, brand, desc1, page in self.products:
            var = tk.BooleanVar(value=(idx == self.current_idx))
            self.check_vars[idx] = var

            row_frame = ttk.Frame(self.check_frame)
            row_frame.pack(fill=tk.X, pady=1)

            cb = ttk.Checkbutton(row_frame, variable=var)
            cb.pack(side=tk.LEFT)

            # Product info
            info = f"#{idx+1}  {oe[:20]}  |  {brand[:12]}  |  {desc1[:25]}  |  第{page}页"
            ttk.Label(row_frame, text=info, font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=4)

            # Highlight current product
            if idx == self.current_idx:
                row_frame.configure(style="Highlight.TFrame")
                # Note: we need a style for this, but for simplicity just add a visual marker
                ttk.Label(row_frame, text="← 当前", font=("Microsoft YaHei", 9, "bold"),
                          foreground="#007AFF").pack(side=tk.LEFT, padx=2)

    def _select_all(self):
        for var in self.check_vars.values():
            var.set(True)

    def _select_none(self):
        for var in self.check_vars.values():
            var.set(False)

    def _skip(self):
        """Skip batch apply - keep only current product."""
        self.destroy()

    def _on_apply(self):
        """Apply the field value to all checked products."""
        selected = [idx for idx, var in self.check_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("未选择", "请至少勾选一个产品", parent=self)
            return
        self.on_apply_callback(self.field_key, self.text, selected)
        self.destroy()


class MainApplication:
    """Top-level application window."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("展会目录PDF自动提取工具")
        self.root.geometry("1400x800")
        self.root.minsize(1024, 600)

        # DPI scaling
        try:
            dpi = self.root.winfo_fpixels('1i') / 96
            if dpi > 1.5:
                self.root.tk.call('tk', 'scaling', dpi)
        except Exception:
            pass

        # ── Theme setup (must happen before any widget creation) ──
        self._setup_theme()

        # State
        self.products = []
        self.edit_state = {}  # {product_idx: {field_key: new_value}}
        self.pdf_path = None
        self.output_dir = None
        self.images_dir = None
        self.all_matches = {}  # {page_num: [match, ...]}

        # Pipeline
        self.pipeline = PipelineRunner()
        self._pipeline_after_id = None

        # Build UI
        self._build_menu()
        self._build_toolbar()
        self._build_main_panels()
        self._build_status_bar()
        self._bind_shortcuts()

        # Start pipeline queue poll
        self._poll_pipeline_queue()

    # ─── Theme ───

    def _setup_theme(self):
        """Configure Apple macOS-style light theme via ttk.Style (clam engine)."""
        style = ttk.Style()
        style.theme_use("clam")

        # ── Apple macOS color palette ──
        BG        = "#F2F2F7"  # iOS/macOS 系统灰底
        BG_PANEL  = "#FFFFFF"  # 白色卡片
        BG_INPUT  = "#FFFFFF"  # 白色输入框
        BG_HOVER  = "#E8E8ED"  # 悬停时微灰
        BG_SELECT = "#0060DF"  # 选中: 深蓝底
        FG        = "#1D1D1F"  # 主文字近黑
        FG_DIM    = "#86868B"  # 次要文字
        FG_WHITE  = "#FFFFFF"  # 白色文字(深蓝底用)
        ACCENT    = "#007AFF"  # Apple系统蓝
        GREEN     = "#34C759"  # Apple绿
        AMBER     = "#FF9500"  # Apple橙
        RED       = "#FF3B30"  # Apple红
        BORDER    = "#E5E5EA"  # 极细分隔线

        # ── Fonts ──
        default_font = ("Microsoft YaHei", 10)
        heading_font = ("Microsoft YaHei", 11, "bold")
        mono_font    = ("Consolas", 10)

        # ── Root-level defaults ──
        self.root.configure(bg=BG)
        style.configure(".", background=BG, foreground=FG, font=default_font,
                        borderwidth=0, troughcolor="#F9F9FC")

        # ── Frame ──
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=BG_PANEL)
        style.configure("Toolbar.TFrame", background=BG)

        # ── Label ──
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Panel.TLabel", background=BG_PANEL, foreground=FG)
        style.configure("Dim.TLabel", foreground=FG_DIM)
        style.configure("Heading.TLabel", font=heading_font)
        style.configure("Panel.Heading.TLabel", background=BG_PANEL, font=heading_font)

        # ── Button: 白底 + 细灰边框，悬停变蓝 ──
        style.configure("TButton",
            background=BG_PANEL, foreground=FG,
            borderwidth=1, bordercolor=BORDER,
            relief="flat", padding=(12, 5),
            font=default_font,
        )
        style.map("TButton",
            background=[("active", ACCENT), ("pressed", "#0060DF")],
            foreground=[("active", FG_WHITE), ("pressed", FG_WHITE)],
            bordercolor=[("active", ACCENT)],
        )
        style.configure("Accent.TButton",
            background=ACCENT, foreground=FG_WHITE,
            borderwidth=0, padding=(14, 6),
        )
        style.map("Accent.TButton",
            background=[("active", "#0060DF"), ("pressed", "#0047B3")],
        )

        # ── Entry: 白底 + 浅灰边框，聚焦变蓝 ──
        style.configure("TEntry",
            fieldbackground=BG_INPUT, foreground=FG,
            borderwidth=1, bordercolor=BORDER,
            relief="solid", padding=(6, 4),
            insertcolor=FG,
        )
        style.map("TEntry",
            bordercolor=[("focus", ACCENT), ("hover", "#C7C7CC")],
        )

        # ── Combobox: 同Entry风格 ──
        style.configure("TCombobox",
            fieldbackground=BG_INPUT, foreground=FG,
            borderwidth=1, bordercolor=BORDER,
            arrowcolor=FG_DIM, relief="solid",
            padding=(4, 3),
        )
        style.map("TCombobox",
            bordercolor=[("focus", ACCENT), ("hover", "#C7C7CC")],
        )
        self.root.option_add("*TCombobox*Listbox.background", BG_PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BG_SELECT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG_WHITE)

        # ── Treeview: 白底卡片式列表 ──
        style.configure("Treeview",
            background=BG_PANEL, foreground=FG,
            fieldbackground=BG_PANEL,
            borderwidth=1, bordercolor=BORDER,
            rowheight=28,
        )
        style.configure("Treeview.Heading",
            background="#FAFAFA", foreground=FG_DIM,
            borderwidth=0, bordercolor=BORDER,
            relief="flat", font=("Microsoft YaHei", 9, "bold"),
            padding=(6, 4),
        )
        style.map("Treeview.Heading",
            background=[("active", "#F0F0F5")],
        )
        style.map("Treeview",
            background=[("selected", BG_SELECT)],
            foreground=[("selected", FG_WHITE)],
        )

        # ── Scrollbar: 半透明浅灰 ──
        style.configure("TScrollbar",
            background="#E5E5EA", troughcolor=BG,
            borderwidth=0, arrowsize=14,
        )
        style.map("TScrollbar",
            background=[("active", "#C7C7CC")],
        )

        # ── Progressbar: 蓝色进度条 ──
        style.configure("TProgressbar",
            troughcolor="#E5E5EA", background=ACCENT,
            bordercolor=BORDER, borderwidth=1,
            thickness=8,
        )

        # ── Separator: 极细浅灰线 ──
        style.configure("TSeparator", background=BORDER)

        # ── Checkbutton ──
        style.configure("TCheckbutton",
            background=BG_PANEL, foreground=FG,
        )
        style.map("TCheckbutton",
            background=[("active", BG_PANEL)],
        )

        # ── PanedWindow sash ──
        style.configure("TPanedwindow", background=BG)
        style.configure("Sash", background=BORDER)
        style.map("Sash", background=[("active", ACCENT)])

        # ── Store palette for use in canvas widgets ──
        self._theme = {
            "bg": BG, "bg_panel": BG_PANEL, "bg_input": BG_INPUT,
            "bg_hover": BG_HOVER, "bg_select": BG_SELECT,
            "fg": FG, "fg_dim": FG_DIM, "fg_white": FG_WHITE,
            "accent": ACCENT, "green": GREEN, "amber": AMBER,
            "red": RED, "border": BORDER,
            "font": default_font, "font_heading": heading_font, "font_mono": mono_font,
        }

    # ─── Menu ───

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="打开PDF...", command=self._on_open_pdf, accelerator="Ctrl+O")
        file_menu.add_command(label="打开JSON...", command=self._on_open_json)
        file_menu.add_separator()
        file_menu.add_command(label="保存项目...", command=self._save_session, accelerator="Ctrl+Shift+S")
        file_menu.add_command(label="加载项目...", command=self._load_session, accelerator="Ctrl+Shift+O")
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.root.quit, accelerator="Ctrl+Q")
        menubar.add_cascade(label="文件", menu=file_menu)

        pipeline_menu = tk.Menu(menubar, tearoff=0)
        pipeline_menu.add_command(label="重新识别", command=self._on_run_pipeline, accelerator="Ctrl+R")
        menubar.add_cascade(label="管线", menu=pipeline_menu)

        export_menu = tk.Menu(menubar, tearoff=0)
        export_menu.add_command(label="导出Excel...", command=self._on_export_excel, accelerator="Ctrl+E")
        export_menu.add_command(label="导出JSON...", command=self._on_export_json, accelerator="Ctrl+S")
        export_menu.add_command(label="导出两者...", command=self._on_export_both)
        menubar.add_cascade(label="导出", menu=export_menu)

        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="字段配置...", command=self._on_field_config)
        menubar.add_cascade(label="设置", menu=settings_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self._on_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menubar)

    # ─── Toolbar ───

    def _build_toolbar(self):
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill=tk.X, padx=4, pady=2)

        ttk.Button(toolbar, text="📂 打开PDF", command=self._on_open_pdf).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="📂 打开JSON", command=self._on_open_json).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="💾 保存项目", command=self._save_session).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="📂 加载项目", command=self._load_session).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)
        ttk.Button(toolbar, text="▶ 重新识别", command=self._on_run_pipeline).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=2)
        ttk.Button(toolbar, text="📊 导出Excel", command=self._on_export_excel).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="📋 导出JSON", command=self._on_export_json).pack(side=tk.LEFT, padx=2)

        self.file_label = ttk.Label(toolbar, text="未打开文件", foreground="#86868B",
                                     font=("Microsoft YaHei", 9))
        self.file_label.pack(side=tk.LEFT, padx=12)

        self.progress = ttk.Progressbar(toolbar, mode="indeterminate", length=100)
        # hidden by default

    # ─── Main 3-Pane Layout ───

    def _build_main_panels(self):
        main_pw = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_pw.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        # Left: PDF Viewer
        pdf_frame = ttk.Frame(main_pw)
        main_pw.add(pdf_frame, weight=4)

        # PDF Canvas with scrollbars
        canvas_container = ttk.Frame(pdf_frame)
        canvas_container.pack(fill=tk.BOTH, expand=True)

        self.pdf_h_scroll = ttk.Scrollbar(canvas_container, orient=tk.HORIZONTAL)
        self.pdf_v_scroll = ttk.Scrollbar(canvas_container, orient=tk.VERTICAL)
        self.pdf_canvas = tk.Canvas(
            canvas_container,
            bg="#E8E8ED",
            xscrollcommand=self.pdf_h_scroll.set,
            yscrollcommand=self.pdf_v_scroll.set,
        )
        self.pdf_h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.pdf_v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.pdf_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # PDF Navigation
        nav_frame = ttk.Frame(pdf_frame)
        nav_frame.pack(fill=tk.X, padx=4, pady=2)
        ttk.Button(nav_frame, text="|<", width=3, command=self._nav_first_page).pack(side=tk.LEFT)
        ttk.Button(nav_frame, text="<", width=3, command=self._nav_prev_page).pack(side=tk.LEFT)
        self.page_label = ttk.Label(nav_frame, text="第 0/0 页", width=12, anchor=tk.CENTER)
        self.page_label.pack(side=tk.LEFT, padx=4)
        ttk.Button(nav_frame, text=">", width=3, command=self._nav_next_page).pack(side=tk.LEFT)
        ttk.Button(nav_frame, text=">|", width=3, command=self._nav_last_page).pack(side=tk.LEFT)

        ttk.Separator(nav_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Label(nav_frame, text="缩放:").pack(side=tk.LEFT)
        self.zoom_var = tk.StringVar(value="100%")
        zoom_combo = ttk.Combobox(nav_frame, textvariable=self.zoom_var,
                                  values=["50%", "75%", "100%", "125%", "150%", "200%", "适应宽度"],
                                  width=8, state="readonly")
        zoom_combo.pack(side=tk.LEFT, padx=2)
        zoom_combo.bind("<<ComboboxSelected>>", self._on_zoom_change)

        # PDF Renderer + Bbox
        self.pdf_renderer = PdfRenderer(self.pdf_canvas, self.pdf_h_scroll, self.pdf_v_scroll)
        self.bbox_overlay = BboxOverlay(self.pdf_canvas)
        self.bbox_overlay.set_click_callback(self._on_bbox_click)

        # Center: Product List
        self.product_list = ProductListPanel(main_pw)
        main_pw.add(self.product_list, weight=3)
        self.product_list.set_select_callback(self._on_product_select)

        # Right: Field Editor
        self.field_editor = FieldEditorPanel(main_pw)
        main_pw.add(self.field_editor, weight=5)
        self.field_editor.set_callbacks(
            on_save=self._on_field_save,
            on_navigate=self._on_field_navigate,
            on_re_extract=self._on_field_re_extract,
            on_re_select_image=self._on_re_select_image,
        )
        self.field_editor.clear()

        # Selection mode state
        self._selection_mode = False
        self._selection_field = None
        self._sel_start_x = 0
        self._sel_start_y = 0
        self._sel_rect_id = None
        self._selection_hint_id = None
        self._ocr_sel_bbox = None  # Persistent OCR region indicator
        self._image_selection_mode = False
        self._image_selection_product_idx = None

    # ─── Status Bar ───

    def _build_status_bar(self):
        self.status_var = tk.StringVar(value="就绪 — 点击 打开PDF 或 打开JSON 开始")
        status_bar = ttk.Frame(self.root, style="Panel.TFrame")
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(self.root, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)
        status_label = ttk.Label(status_bar, textvariable=self.status_var,
                                 anchor=tk.W, font=("Microsoft YaHei", 9), foreground="#86868B",
                                 padding=(10, 3))
        status_label.pack(fill=tk.X)

    # ─── Keyboard Shortcuts ───

    def _bind_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self._on_open_pdf())
        self.root.bind("<Control-s>", lambda e: self._on_export_json())
        self.root.bind("<Control-S>", lambda e: self._save_session())  # Ctrl+Shift+S
        self.root.bind("<Control-O>", lambda e: self._load_session())  # Ctrl+Shift+O
        self.root.bind("<Control-e>", lambda e: self._on_export_excel())
        self.root.bind("<Control-r>", lambda e: self._on_run_pipeline())
        self.root.bind("<Control-n>", lambda e: self.product_list.select_next())
        self.root.bind("<Control-p>", lambda e: self.product_list.select_prev())
        self.root.bind("<Control-q>", lambda e: self.root.quit())
        self.root.bind("<Escape>", lambda e: (
            self._exit_selection_mode() if self._selection_mode else
            self._exit_image_selection_mode() if getattr(self, '_image_selection_mode', False) else None
        ))

    # ─── File Operations ───

    def _on_open_pdf(self):
        """Open a PDF file and run the extraction pipeline."""
        filepath = filedialog.askopenfilename(
            title="选择PDF文件",
            filetypes=[("PDF Files", "*.pdf"), ("All Files", "*.*")],
        )
        if not filepath:
            return

        self.pdf_path = filepath
        output_dir = os.path.join(os.path.dirname(filepath), "..", "step5_output")
        pdf_name = Path(filepath).stem
        self.output_dir = os.path.abspath(output_dir)
        self.file_label.config(text=f"{os.path.basename(filepath)}")

        # Show progress
        self.progress.pack(side=tk.LEFT, padx=6)
        self.progress.start()
        self.status_var.set("正在运行提取管线...")

        # Run pipeline in background
        self.pipeline.run(filepath, self.output_dir)

    def _on_open_json(self):
        """Open a previously saved fields JSON file."""
        filepath = filedialog.askopenfilename(
            title="选择JSON字段文件",
            filetypes=[("JSON Files", "*_fields.json"), ("All Files", "*.*")],
        )
        if not filepath:
            return

        try:
            products, report, json_path, images_dir, all_matches = self.pipeline.load_from_json(filepath)
            self._on_pipeline_complete(products, report, json_path, None, images_dir, all_matches)
            self.file_label.config(text=f"{os.path.basename(filepath)}")
            self.status_var.set(f"已加载: {os.path.basename(filepath)}")
        except Exception as e:
            messagebox.showerror("加载失败", f"无法加载JSON文件:\n{e}")

    def _on_run_pipeline(self):
        """Re-run pipeline on current PDF."""
        if not self.pdf_path:
            messagebox.showwarning("未选择文件", "请先打开一个PDF文件")
            return
        self.progress.pack(side=tk.LEFT, padx=6)
        self.progress.start()
        self.status_var.set("正在重新运行提取管线...")
        self.pipeline.run(self.pdf_path, self.output_dir)

    # ─── Pipeline Callbacks ───

    def _poll_pipeline_queue(self):
        """Poll the pipeline queue for progress updates."""
        try:
            while True:
                msg = self.pipeline.queue.get_nowait()
                msg_type = msg[0]

                if msg_type == "progress":
                    self.status_var.set(msg[1])
                elif msg_type == "warning":
                    self.status_var.set(f"⚠ {msg[1]}")
                elif msg_type == "status_update":
                    _, status_text = msg
                    self.status_var.set(status_text)
                elif msg_type == "ocr_done":
                    _, field_key, text, product_idx, pdf_bbox = msg
                    self._on_ocr_done(field_key, text, product_idx, pdf_bbox)
                elif msg_type == "ocr_error":
                    _, field_key, error = msg
                    self._on_ocr_error(field_key, error)
                elif msg_type == "image_extracted":
                    _, product_idx, image_path = msg
                    self._on_image_extracted(product_idx, image_path)
                elif msg_type == "image_error":
                    _, product_idx, error = msg
                    self._on_image_error(product_idx, error)
                elif msg_type == "done":
                    _, products, report, fields_path, excel_path, images_dir, *rest = msg
                    all_matches = rest[0] if rest else {}
                    self._on_pipeline_complete(products, report, fields_path, excel_path, images_dir, all_matches)
                elif msg_type == "error":
                    self.progress.stop()
                    self.progress.pack_forget()
                    self.status_var.set("管线运行失败")
                    messagebox.showerror("管线错误", f"提取过程出错:\n{msg[1]}")
        except queue.Empty:
            pass

        # Continue polling
        self._pipeline_after_id = self.root.after(200, self._poll_pipeline_queue)

    def _on_pipeline_complete(self, products: list, report: dict,
                               fields_path: str, excel_path: str | None,
                               images_dir: str, all_matches: dict = None):
        """Handle pipeline completion."""
        self.progress.stop()
        self.progress.pack_forget()

        self.products = products
        self.edit_state = {}
        self.images_dir = images_dir
        self.all_matches = all_matches or {}
        self.field_editor.set_images_dir(images_dir or "")

        # Load PDF for rendering
        if self.pdf_path and fitz:
            if not self.pdf_renderer.doc:
                self.pdf_renderer.open_pdf(self.pdf_path)
            self.pdf_renderer.go_to_page(0)
            self._update_page_label()

        # Load product list
        self.product_list.load_products(products, self.edit_state)

        # Select first product
        if products:
            self.product_list.select_product(0)

        # Update status
        total = report.get("total_products", len(products))
        pages = report.get("total_pages", "?")
        avg_conf = report.get("avg_confidence", 0)
        self.status_var.set(
            f"提取完成 | 产品: {total} | 页数: {pages} | 平均置信度: {avg_conf:.0%}"
        )

    # ─── Product Selection ───

    def _on_product_select(self, product_idx: int):
        """Handle product selection in the list."""
        # Exit selection mode if active
        if self._selection_mode:
            self._exit_selection_mode()
        if getattr(self, '_image_selection_mode', False):
            self._exit_image_selection_mode()

        # Clear any OCR region indicators from previous selection
        self.pdf_canvas.delete("ocr_region")
        self._ocr_sel_bbox = None

        if product_idx >= len(self.products):
            return

        product = self.products[product_idx]
        self.field_editor.populate(product, product_idx, self.edit_state)

        # Navigate PDF to product's page
        product_page = product.get("page", 1) - 1  # 0-indexed
        if self.pdf_renderer.doc and self.pdf_renderer.current_page != product_page:
            self.pdf_renderer.go_to_page(product_page)
            self._update_page_label()

        # Show only the selected product's bbox
        self._redraw_bboxes(product_idx)

        # Auto-scroll canvas to center on the product's bbox
        self._scroll_to_bbox(product_idx)

    def _scroll_to_bbox(self, product_idx: int):
        """Scroll the PDF canvas to center on a product's card_bbox."""
        if not self.pdf_renderer.doc:
            return
        product = self.products[product_idx]
        bbox = product.get("card_bbox")
        if not bbox:
            return
        zoom = self.pdf_renderer.zoom
        # Center of bbox in canvas coordinates
        cx = (bbox[0] + bbox[2]) / 2 * zoom
        cy = (bbox[1] + bbox[3]) / 2 * zoom
        # Get scrollregion from canvas
        try:
            sr = self.pdf_canvas.cget("scrollregion")
            if not sr:
                return
            parts = sr.split()
            total_w = float(parts[2])
            total_h = float(parts[3])
        except Exception:
            return
        cw = self.pdf_canvas.winfo_width()
        ch = self.pdf_canvas.winfo_height()
        if cw > 0 and ch > 0 and total_w > 0 and total_h > 0:
            frac_x = max(0.0, min(1.0, (cx - cw / 2) / total_w))
            frac_y = max(0.0, min(1.0, (cy - ch / 2) / total_h))
            self.pdf_canvas.xview_moveto(frac_x)
            self.pdf_canvas.yview_moveto(frac_y)

    def _on_bbox_click(self, product_idx: int):
        """Handle click on a bbox in the PDF viewer."""
        self.product_list.select_product(product_idx)

    # ─── Field Editing ───

    # ─── Interactive Re-Extraction (Selection Mode) ───

    # ─── Image Re-Selection Mode ───

    def _on_re_select_image(self, product_idx: int):
        """Enter PDF selection mode for re-selecting a product image."""
        if not self.pdf_renderer.doc:
            messagebox.showinfo("提示", "请先打开PDF文件")
            return

        # Navigate to the product's page
        product = self.products[product_idx]
        product_page = product.get("page", 1) - 1
        if self.pdf_renderer.current_page != product_page:
            self.pdf_renderer.go_to_page(product_page)
            self._update_page_label()
        # Redraw bboxes for this page so the user sees product positions
        self._redraw_bboxes()

        # Clear any previous OCR region indicators
        self.pdf_canvas.delete("ocr_region")
        self._ocr_sel_bbox = None

        # Exit any existing OCR selection mode
        if self._selection_mode:
            self._exit_selection_mode()

        # Enter image selection mode
        self._image_selection_mode = True
        self._image_selection_product_idx = product_idx

        # Change cursor
        self.pdf_canvas.config(cursor="crosshair")

        # Show hint
        hint_text = "请在左侧PDF页面框选该产品对应的图片区域"
        self._selection_hint_id = self.pdf_canvas.create_text(
            10, 10, text=hint_text, anchor=tk.NW,
            fill="#34C759", font=("Microsoft YaHei", 10, "bold"),
            tags=("selection_hint",),
        )
        bbox = self.pdf_canvas.bbox(self._selection_hint_id)
        if bbox:
            self.pdf_canvas.create_rectangle(
                bbox[0]-4, bbox[1]-2, bbox[2]+4, bbox[3]+2,
                fill="#E8F5E9", outline="", tags=("selection_hint",),
            )
            self.pdf_canvas.tag_raise(self._selection_hint_id)

        # Bind mouse events
        self.pdf_canvas.bind("<Button-1>", self._on_img_sel_start)
        self.pdf_canvas.bind("<B1-Motion>", self._on_img_sel_drag)
        self.pdf_canvas.bind("<ButtonRelease-1>", self._on_img_sel_end)

        # Unbind normal bbox click during selection
        self.pdf_canvas.tag_unbind("bbox_rect", "<Button-1>")

        self.status_var.set("图片选区模式: 框选产品图片区域 (按 Esc 取消)")

    def _exit_image_selection_mode(self):
        """Exit image selection mode."""
        self._image_selection_mode = getattr(self, '_image_selection_mode', False)
        self._image_selection_product_idx = getattr(self, '_image_selection_product_idx', None)

        self.pdf_canvas.config(cursor="")
        self.pdf_canvas.delete("selection_hint")
        self.pdf_canvas.delete("sel_rect")

        self.pdf_canvas.unbind("<Button-1>")
        self.pdf_canvas.unbind("<B1-Motion>")
        self.pdf_canvas.unbind("<ButtonRelease-1>")

        # Re-bind bbox click
        self.pdf_canvas.tag_bind("bbox_rect", "<Button-1>", self.bbox_overlay._on_bbox_click)

        self._image_selection_mode = False
        self.status_var.set("已取消图片选区模式")

    def _on_img_sel_start(self, event):
        """Mouse down in image selection mode."""
        if not getattr(self, '_image_selection_mode', False):
            return
        x = self.pdf_canvas.canvasx(event.x)
        y = self.pdf_canvas.canvasy(event.y)
        self._sel_start_x = x
        self._sel_start_y = y
        self.pdf_canvas.delete("sel_rect")
        self._sel_rect_id = self.pdf_canvas.create_rectangle(
            x, y, x, y,
            outline="#34C759", width=2, dash=(4, 2),
            tags=("sel_rect",),
        )

    def _on_img_sel_drag(self, event):
        """Mouse drag in image selection mode."""
        if not getattr(self, '_image_selection_mode', False) or self._sel_rect_id is None:
            return
        x = self.pdf_canvas.canvasx(event.x)
        y = self.pdf_canvas.canvasy(event.y)
        self.pdf_canvas.coords(
            self._sel_rect_id,
            self._sel_start_x, self._sel_start_y, x, y,
        )

    def _on_img_sel_end(self, event):
        """Mouse release in image selection mode - extract image region."""
        if not getattr(self, '_image_selection_mode', False):
            return
        x = self.pdf_canvas.canvasx(event.x)
        y = self.pdf_canvas.canvasy(event.y)

        x0 = min(self._sel_start_x, x)
        y0 = min(self._sel_start_y, y)
        x1 = max(self._sel_start_x, x)
        y1 = max(self._sel_start_y, y)

        if (x1 - x0) < 15 or (y1 - y0) < 15:
            self.status_var.set("选区太小，请重新框选 (按 Esc 取消)")
            return

        # Convert to PDF coords
        zoom = self.pdf_renderer.zoom
        pdf_bbox = [v / zoom for v in [x0, y0, x1, y1]]

        product_idx = self._image_selection_product_idx

        # Save for visual indicator
        self._ocr_sel_bbox = pdf_bbox[:]

        # Exit image selection mode
        self._exit_image_selection_mode()

        # Extract image from PDF region
        self._extract_image_region(pdf_bbox, product_idx)

    def _extract_image_region(self, pdf_bbox: list, product_idx: int):
        """Extract image from PDF region, remove overlay text, and associate with product."""
        if not self.pdf_renderer.doc:
            return

        self.status_var.set("正在提取图片并消除文字...")
        self.progress.pack(side=tk.LEFT, padx=6)
        self.progress.start()

        # Lazy singleton OCR for text removal (avoid re-init each call)
        if not hasattr(self, '_text_removal_ocr'):
            self._text_removal_ocr = None

        def _remove_text_from_image(pil_image):
            """Detect and remove OCR text from product image using inpainting."""
            import numpy as np
            try:
                import cv2
            except ImportError:
                cv2 = None

            try:
                # Lazy init PaddleOCR once (takes ~3s first time)
                if self._text_removal_ocr is None:
                    try:
                        from paddleocr import PaddleOCR
                        self._text_removal_ocr = PaddleOCR(
                            lang='ch', show_log=False,
                            det_db_thresh=0.2,           # 更低阈值，检测不完整文字
                            det_db_box_thresh=0.15,       # 更低box阈值
                            det_db_unclip_ratio=1.8,      # 扩展检测框以覆盖断裂笔画
                            drop_score=0.2,               # 接受低置信度识别（部分可见的文字）
                        )
                    except Exception:
                        return pil_image  # OCR not available

                img_array = np.array(pil_image)
                h, w = img_array.shape[:2]

                # Downsample if image is very large for faster OCR
                ocr_scale = 1.0
                if max(w, h) > 1200:
                    ocr_scale = 1200.0 / max(w, h)
                    ocr_w, ocr_h = int(w * ocr_scale), int(h * ocr_scale)
                    ocr_img = np.array(pil_image.resize((ocr_w, ocr_h), Image.LANCZOS))
                else:
                    ocr_img = img_array

                results = self._text_removal_ocr.ocr(ocr_img, cls=False)
                if not results or not results[0]:
                    return pil_image  # No text detected

                # Build mask at original resolution
                mask = np.zeros((h, w), dtype=np.uint8)

                for line in results[0]:
                    bbox = line[0]  # [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
                    # Scale bbox back to original resolution
                    if ocr_scale != 1.0:
                        bbox = [[p[0] / ocr_scale, p[1] / ocr_scale] for p in bbox]

                    # Expand bbox outward to catch partial chars at selection edges
                    margin = 8  # pixels
                    x_vals = [p[0] for p in bbox]
                    y_vals = [p[1] for p in bbox]
                    cx, cy = sum(x_vals) / 4, sum(y_vals) / 4
                    expanded = []
                    for p in bbox:
                        dx = p[0] - cx
                        dy = p[1] - cy
                        expanded.append([
                            max(0, min(w, p[0] + margin * (1 if dx >= 0 else -1))),
                            max(0, min(h, p[1] + margin * (1 if dy >= 0 else -1))),
                        ])
                    bbox = expanded

                    pts = np.array(bbox, dtype=np.int32)
                    if cv2 is not None:
                        cv2.fillPoly(mask, [pts], 255)
                    else:
                        x_vals = [int(p[0]) for p in bbox]
                        y_vals = [int(p[1]) for p in bbox]
                        x0, x1 = max(0, min(x_vals)-2), min(w, max(x_vals)+2)
                        y0, y1 = max(0, min(y_vals)-2), min(h, max(y_vals)+2)
                        mask[y0:y1, x0:x1] = 255

                if mask.sum() == 0:
                    return pil_image

                # Inpaint text regions (more aggressive dilation for partial chars)
                if cv2 is not None:
                    kernel = np.ones((5, 5), np.uint8)
                    mask = cv2.dilate(mask, kernel, iterations=4)
                    result = cv2.inpaint(img_array, mask, inpaintRadius=7,
                                         flags=cv2.INPAINT_TELEA)
                    return Image.fromarray(result)
                else:
                    # Pure numpy fallback: fill with background color
                    bg_mask = (mask == 0)
                    bg_color = np.median(img_array[bg_mask], axis=0).astype(np.uint8) if bg_mask.sum() > 100 else np.array([255, 255, 255], dtype=np.uint8)
                    result = img_array.copy()
                    result[mask > 0] = bg_color
                    return Image.fromarray(result)

            except Exception:
                return pil_image  # On any error, return original

        def _do_extract():
            try:
                page = self.pdf_renderer.doc[self.pdf_renderer.current_page]
                # Render at PDF-native resolution (~432 DPI = 6x PDF base 72 DPI)
                extract_zoom = 6.0
                mat = fitz.Matrix(extract_zoom, extract_zoom)
                clip = fitz.Rect(pdf_bbox)
                pix = page.get_pixmap(matrix=mat, clip=clip)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                # Remove overlay text from product image
                self.pipeline.queue.put(("status_update", "正在消除文字..."))
                img = _remove_text_from_image(img)

                # Save image
                pdf_name = Path(self.pdf_path).stem if self.pdf_path else "unknown"
                save_dir = os.path.join(
                    os.path.dirname(self.pdf_path) if self.pdf_path else ".",
                    "..", "step5_output", pdf_name, f"{pdf_name}_images",
                )
                os.makedirs(save_dir, exist_ok=True)
                img_filename = f"{pdf_name}_product_{product_idx+1}_manual.png"
                img_path = os.path.join(save_dir, img_filename)
                img.save(img_path, format="PNG")

                self.pipeline.queue.put(("image_extracted", product_idx, img_path))
            except Exception as e:
                self.pipeline.queue.put(("image_error", product_idx, str(e)))

        threading.Thread(target=_do_extract, daemon=True).start()

    def _on_image_extracted(self, product_idx: int, image_path: str):
        """Handle successful image extraction."""
        self.progress.stop()
        self.progress.pack_forget()

        # Store in edit_state
        if product_idx not in self.edit_state:
            self.edit_state[product_idx] = {}
        self.edit_state[product_idx]["_product_image"] = image_path

        # Update product's matched images
        product = self.products[product_idx]
        if "_matched_images" not in product:
            product["_matched_images"] = []
        # Insert manual image at front
        product["_matched_images"].insert(0, {
            "xref": -1,
            "filename": os.path.basename(image_path),
            "bbox": [0, 0, 0, 0],
            "confidence": 1.0,
            "method": "manual_selection",
        })

        # Refresh field editor to show new image
        self.field_editor.set_images_dir(os.path.dirname(image_path))
        self.field_editor.populate(product, product_idx, self.edit_state)

        # Update product list
        self.product_list.update_row(product_idx)

        # Draw visual indicator on PDF
        if self._ocr_sel_bbox:
            self._draw_ocr_region(self._ocr_sel_bbox, "产品图片")
        self.status_var.set(f"图片已提取: {os.path.basename(image_path)} → 产品 #{product_idx+1}")

    def _on_image_error(self, product_idx: int, error: str):
        """Handle image extraction error."""
        self.progress.stop()
        self.progress.pack_forget()
        self.status_var.set(f"图片提取失败: {error}")
        messagebox.showerror("图片提取错误", f"无法提取图片:\n{error}")

    # ─── OCR Text Re-Extraction ───

    def _on_field_re_extract(self, field_key: str):
        """Enter PDF selection mode for re-extracting a specific field."""
        if not self.pdf_renderer.doc:
            messagebox.showinfo("提示", "请先打开PDF文件")
            return

        # Clear previous OCR region indicators
        self.pdf_canvas.delete("ocr_region")
        self._ocr_sel_bbox = None

        self._selection_mode = True
        self._selection_field = field_key

        # Change cursor
        self.pdf_canvas.config(cursor="crosshair")

        # Highlight the button
        self.field_editor.highlight_re_extract_button(field_key, True)

        # Show hint on canvas
        hint_text = f"请在左侧PDF页面框选 '{field_key}' 字段对应的文本区域"
        self._selection_hint_id = self.pdf_canvas.create_text(
            10, 10, text=hint_text, anchor=tk.NW,
            fill="#007AFF", font=("Microsoft YaHei", 10, "bold"),
            tags=("selection_hint",),
        )
        # Draw a background for the hint
        bbox = self.pdf_canvas.bbox(self._selection_hint_id)
        if bbox:
            self.pdf_canvas.create_rectangle(
                bbox[0]-4, bbox[1]-2, bbox[2]+4, bbox[3]+2,
                fill="#E3F2FD", outline="", tags=("selection_hint",),
            )
            self.pdf_canvas.tag_raise(self._selection_hint_id)

        # Bind mouse events
        self.pdf_canvas.bind("<Button-1>", self._on_sel_start)
        self.pdf_canvas.bind("<B1-Motion>", self._on_sel_drag)
        self.pdf_canvas.bind("<ButtonRelease-1>", self._on_sel_end)

        # Unbind normal bbox click during selection
        self.pdf_canvas.tag_unbind("bbox_rect", "<Button-1>")

        self.status_var.set(f"选区模式: 框选 '{field_key}' 字段的文本区域 (按 Esc 取消)")

    def _exit_selection_mode(self):
        """Exit selection mode without applying."""
        self._selection_mode = False
        self._selection_field = None

        self.pdf_canvas.config(cursor="")
        self.pdf_canvas.delete("selection_hint")
        self.pdf_canvas.delete("sel_rect")

        # Unbind selection events
        self.pdf_canvas.unbind("<Button-1>")
        self.pdf_canvas.unbind("<B1-Motion>")
        self.pdf_canvas.unbind("<ButtonRelease-1>")

        # Re-bind bbox click
        self.pdf_canvas.tag_bind("bbox_rect", "<Button-1>", self.bbox_overlay._on_bbox_click)

        # Reset button highlight
        if hasattr(self.field_editor, 'highlight_re_extract_button') and self._selection_field:
            self.field_editor.highlight_re_extract_button(self._selection_field, False)

        self.status_var.set("已取消选区模式")

    def _on_sel_start(self, event):
        """Mouse down in selection mode - start rectangle."""
        if not self._selection_mode:
            return
        # Convert to canvas coordinates
        x = self.pdf_canvas.canvasx(event.x)
        y = self.pdf_canvas.canvasy(event.y)
        self._sel_start_x = x
        self._sel_start_y = y

        # Remove previous selection rectangle
        self.pdf_canvas.delete("sel_rect")

        # Create new rectangle
        self._sel_rect_id = self.pdf_canvas.create_rectangle(
            x, y, x, y,
            outline="#007AFF", width=2, dash=(4, 2),
            tags=("sel_rect",),
        )

    def _on_sel_drag(self, event):
        """Mouse drag in selection mode - update rectangle."""
        if not self._selection_mode or self._sel_rect_id is None:
            return
        x = self.pdf_canvas.canvasx(event.x)
        y = self.pdf_canvas.canvasy(event.y)
        self.pdf_canvas.coords(
            self._sel_rect_id,
            self._sel_start_x, self._sel_start_y, x, y,
        )

    def _on_sel_end(self, event):
        """Mouse release in selection mode - finalize and run OCR."""
        if not self._selection_mode:
            return
        x = self.pdf_canvas.canvasx(event.x)
        y = self.pdf_canvas.canvasy(event.y)

        # Calculate bbox (ensure x0 < x1, y0 < y1)
        x0 = min(self._sel_start_x, x)
        y0 = min(self._sel_start_y, y)
        x1 = max(self._sel_start_x, x)
        y1 = max(self._sel_start_y, y)

        # Minimum selection size check
        if (x1 - x0) < 10 or (y1 - y0) < 10:
            self.status_var.set("选区太小，请重新框选 (按 Esc 取消)")
            return

        # Convert canvas coords to PDF coords (un-zoom)
        zoom = self.pdf_renderer.zoom
        pdf_bbox = [v / zoom for v in [x0, y0, x1, y1]]

        # Save selection bbox for persistent visual feedback
        self._ocr_sel_bbox = pdf_bbox[:]

        # Exit selection mode
        self._selection_mode = False
        self._selection_field_key = self._selection_field
        self._exit_selection_mode_clean()

        # Run OCR on selected region
        self._run_ocr_on_region(pdf_bbox)

    def _exit_selection_mode_clean(self):
        """Clean up selection mode UI without affecting field_key tracking."""
        self.pdf_canvas.config(cursor="")
        self.pdf_canvas.delete("selection_hint")
        self.pdf_canvas.delete("sel_rect")

        self.pdf_canvas.unbind("<Button-1>")
        self.pdf_canvas.unbind("<B1-Motion>")
        self.pdf_canvas.unbind("<ButtonRelease-1>")

        # Re-bind bbox click
        self.pdf_canvas.tag_bind("bbox_rect", "<Button-1>", self.bbox_overlay._on_bbox_click)

        # Reset button highlight
        if hasattr(self.field_editor, 'highlight_re_extract_button') and self._selection_field:
            self.field_editor.highlight_re_extract_button(self._selection_field, False)

    def _run_ocr_on_region(self, pdf_bbox: list):
        """Run OCR on a selected PDF region and fill the target field."""
        field_key = self._selection_field_key
        product_idx = self.field_editor.current_product_idx
        current_page = self.pdf_renderer.current_page
        self._selection_field_key = None

        if not self.pdf_renderer.doc:
            self.status_var.set("OCR失败: PDF未打开")
            return

        self.status_var.set("正在对选区进行OCR识别...")
        self.progress.pack(side=tk.LEFT, padx=6)
        self.progress.start()

        def _join_ocr_results_spatially(detections: list) -> str:
            """Join PaddleOCR detections preserving natural word spacing.

            Strategy:
            - Latin-script detections (most auto parts text): ALWAYS insert a space
              between adjacent detections on the same line, since PaddleOCR detects
              at word level for Latin text.
            - CJK detections: only insert space when there's a visible horizontal gap,
              since CJK text doesn't use inter-word spacing.
            - Different lines: join with a single space (simulating a line break).
            """
            if not detections:
                return ""

            def _has_cjk(s: str) -> bool:
                """Check if string contains CJK characters."""
                for c in s:
                    cp = ord(c)
                    if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                            0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F):
                        return True
                return False

            # Collect detections with position info
            items = []
            for det in detections:
                bbox = det[0]
                txt = det[1][0]
                if not txt:
                    continue
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                items.append({
                    'x': min(xs), 'y': min(ys),
                    'w': max(xs) - min(xs), 'h': max(ys) - min(ys),
                    'text': txt,
                })
            if not items:
                return ""

            # Sort: top-to-bottom, left-to-right
            items.sort(key=lambda d: (d['y'], d['x']))

            # Group into lines by Y proximity
            # Use a generous threshold so words on the same line aren't split
            avg_h = sum(d['h'] for d in items) / len(items)
            line_threshold = avg_h * 0.65
            lines = []
            current_line = [items[0]]
            for d in items[1:]:
                if abs(d['y'] - current_line[-1]['y']) < line_threshold:
                    current_line.append(d)
                else:
                    lines.append(current_line)
                    current_line = [d]
            lines.append(current_line)

            # Join each line
            result_lines = []
            for line_items in lines:
                line_items.sort(key=lambda d: d['x'])
                line_text = ""
                for i, d in enumerate(line_items):
                    if i > 0:
                        prev = line_items[i - 1]
                        prev_cjk = _has_cjk(prev['text'])
                        curr_cjk = _has_cjk(d['text'])
                        if not prev_cjk and not curr_cjk:
                            # Both Latin: always insert space (word-level detection)
                            line_text += " "
                        else:
                            # At least one CJK: check spatial gap
                            gap = d['x'] - (prev['x'] + prev['w'])
                            avg_char_w = prev['w'] / max(len(prev['text']), 1)
                            if gap > avg_char_w * 0.2:
                                line_text += " "
                    line_text += d['text']
                result_lines.append(line_text)

            # Post-process: fix concatenated Latin words that PaddleOCR merged
            # e.g. "Type-RGrilleForSi21+" → "Type-R Grille For Si 21+"
            processed_lines = []
            for line_text in result_lines:
                if not _has_cjk(line_text):
                    # Only on pure-Latin lines (safe for product codes)
                    # lowercase→uppercase: "GrilleFor" → "Grille For"
                    line_text = re.sub(r'([a-z])([A-Z])', r'\1 \2', line_text)
                    # letter→digit: "Si21" → "Si 21"
                    line_text = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', line_text)
                processed_lines.append(line_text)

            return "\n".join(processed_lines)  # Preserve line breaks for multi-line parsing

        def _do_ocr():
            try:
                page = self.pdf_renderer.doc[current_page]
                ocr_zoom = 3.0
                mat = fitz.Matrix(ocr_zoom, ocr_zoom)
                clip = fitz.Rect(pdf_bbox)
                pix = page.get_pixmap(matrix=mat, clip=clip)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                text = ""
                try:
                    from paddleocr import PaddleOCR
                    import numpy as np
                    ocr = PaddleOCR(lang='ch', show_log=False,
                                    det_db_thresh=0.2,
                                    det_db_box_thresh=0.15)
                    img_array = np.array(img)
                    results = ocr.ocr(img_array, cls=False)
                    if results and results[0]:
                        text = _join_ocr_results_spatially(results[0])
                except Exception:
                    pass

                if not text:
                    text = "(OCR未识别到文本)"

                self.pipeline.queue.put(("ocr_done", field_key, text.strip(), product_idx, pdf_bbox))
            except Exception as e:
                self.pipeline.queue.put(("ocr_error", field_key, str(e)))

        threading.Thread(target=_do_ocr, daemon=True).start()

    def _on_ocr_done(self, field_key: str, text: str, product_idx: int, pdf_bbox: list):
        """Handle OCR completion - fill field(s) and auto-save.

        Smart multi-line mode: when OCR text contains multiple lines AND the
        target field belongs to the text group (OE / desc_1 / desc_2 / desc_3),
        lines are auto-distributed:
          line 0 → oe_number, line 1 → desc_1, line 2 → desc_2, line 3+ → desc_3
        This lets the user select an entire product text block and fix all fields at once.
        """
        self.progress.stop()
        self.progress.pack_forget()

        current_idx = self.field_editor.current_product_idx
        if current_idx is None or current_idx != product_idx:
            self.status_var.set(f"OCR完成但产品已切换，结果丢弃: '{text[:30]}'")
            return

        # Detect multi-line text for smart distribution
        lines = text.split('\n')
        is_multi_line = len(lines) >= 2 and field_key in TEXT_GROUP_FIELDS

        if is_multi_line:
            # ── Smart multi-line distribution ──
            # Map lines to text-group fields in order
            updates = {}  # field_key → text
            for i, line_text in enumerate(lines):
                line_text = line_text.strip()
                if not line_text:
                    continue
                if i < len(TEXT_GROUP_FIELDS):
                    updates[TEXT_GROUP_FIELDS[i]] = line_text
                else:
                    # Extra lines → append to description_3
                    existing = updates.get("description_3", "")
                    updates["description_3"] = (existing + " " + line_text).strip()

            product = self.products[product_idx]
            updated_fields = []

            for fk, fv in updates.items():
                # Fill the corresponding entry widget
                entry = self.field_editor.field_entries.get(fk)
                if entry:
                    entry.delete(0, tk.END)
                    entry.insert(0, fv)

                # Save to edit_state
                field_data = product.get(fk, {})
                original = field_data.get("value", "") if isinstance(field_data, dict) else str(field_data) if field_data else ""
                if fv != original:
                    if product_idx not in self.edit_state:
                        self.edit_state[product_idx] = {}
                    self.edit_state[product_idx][fk] = fv
                    updated_fields.append(fk)

            if updated_fields:
                self._recalc_confidence(product_idx)

            # Update center table
            self.product_list.update_row(product_idx)

            # Re-populate all field entries
            self.field_editor.populate(product, product_idx, self.edit_state)

            # Draw OCR region indicator
            self._draw_ocr_region(pdf_bbox, "文本组团")
            self.status_var.set(
                f"智能解析: {' | '.join(f'{k}={v[:20]}' for k,v in updates.items())}"
            )
            # No batch dialog for multi-line (all fields already applied)
        else:
            # ── Single-field OCR (original flow) ──
            entry = self.field_editor.field_entries.get(field_key)
            if entry:
                entry.delete(0, tk.END)
                entry.insert(0, text)
            else:
                self.status_var.set(f"OCR完成但未找到字段: {field_key}")
                return

            product = self.products[product_idx]
            field_data = product.get(field_key, {})
            original = field_data.get("value", "") if isinstance(field_data, dict) else str(field_data) if field_data else ""

            if text != original:
                if product_idx not in self.edit_state:
                    self.edit_state[product_idx] = {}
                self.edit_state[product_idx][field_key] = text
                self._recalc_confidence(product_idx)

            self.product_list.update_row(product_idx)
            self.field_editor.populate(product, product_idx, self.edit_state)

            field_label = next((fd["label"] for fd in FIELD_DEFINITIONS if fd["key"] == field_key), field_key)
            self._draw_ocr_region(pdf_bbox, field_label)
            self.status_var.set(f"OCR完成: '{text[:40]}' → {field_key} (已自动保存)")

            # Show batch-apply dialog for single-field OCR
            self._show_batch_apply_dialog(field_key, field_label, text, product_idx)

    def _draw_ocr_region(self, pdf_bbox: list, label: str):
        """Draw persistent OCR region indicator on PDF canvas."""
        self._ocr_sel_bbox = pdf_bbox
        zoom = self.pdf_renderer.zoom
        x0, y0, x1, y1 = [v * zoom for v in pdf_bbox]
        self.pdf_canvas.delete("ocr_region")
        self.pdf_canvas.create_rectangle(
            x0, y0, x1, y1,
            outline="#007AFF", width=2, dash=(3, 3),
            tags=("ocr_region",),
        )
        self.pdf_canvas.create_text(
            x0 + 4, y0 - 2, text=f"OCR: {label}", anchor=tk.SW,
            fill="#007AFF", font=("Microsoft YaHei", 9, "bold"),
            tags=("ocr_region",),
        )

    def _show_batch_apply_dialog(self, field_key: str, field_label: str, text: str, current_idx: int):
        """Show dialog to batch-apply OCR result to other products on the same page."""
        current_page = self.products[current_idx].get("page", 1)

        # Collect same-page products
        same_page = []
        for idx, p in enumerate(self.products):
            if p.get("page") == current_page:
                oe = p.get("oe_number", {}).get("value", "") if isinstance(p.get("oe_number"), dict) else str(p.get("oe_number", ""))
                brand = p.get("brand", {}).get("value", "") if isinstance(p.get("brand"), dict) else str(p.get("brand", ""))
                d1 = p.get("description_1", {}).get("value", "") if isinstance(p.get("description_1"), dict) else str(p.get("description_1", ""))
                same_page.append((idx, oe, brand, d1, current_page))

        if len(same_page) <= 1:
            return  # No other products on this page

        # Show dialog (non-modal to avoid blocking UI thread)
        BatchApplyDialog(
            self.root, field_key, field_label, text,
            same_page, current_idx, self.edit_state,
            on_apply_callback=self._on_batch_apply,
        )

    def _on_batch_apply(self, field_key: str, text: str, selected_indices: list):
        """Apply a field value to multiple products at once."""
        updated_count = 0
        for idx in selected_indices:
            product = self.products[idx]
            field_data = product.get(field_key, {})
            original = field_data.get("value", "") if isinstance(field_data, dict) else str(field_data) if field_data else ""

            if text != original:
                if idx not in self.edit_state:
                    self.edit_state[idx] = {}
                self.edit_state[idx][field_key] = text
                self._recalc_confidence(idx)
                updated_count += 1

        # Refresh center table for all affected products
        for idx in selected_indices:
            self.product_list.update_row(idx)

        # Re-populate field editor for current product (in case it was one of the updated)
        current_idx = self.field_editor.current_product_idx
        if current_idx is not None and current_idx < len(self.products):
            self.field_editor.populate(self.products[current_idx], current_idx, self.edit_state)

        self.status_var.set(f"批量应用完成: '{text[:30]}' → {updated_count}个产品的 {field_key}")

    def _on_ocr_error(self, field_key: str, error: str):
        """Handle OCR error."""
        self.progress.stop()
        self.progress.pack_forget()
        self.status_var.set(f"OCR失败: {error}")
        messagebox.showerror("OCR错误", f"文字识别失败:\n{error}")

    def _on_field_save(self, product_idx: int, values: dict):
        """Save edited field values."""
        if product_idx >= len(self.products):
            return

        product = self.products[product_idx]
        has_changes = False

        for key, new_value in values.items():
            field_data = product.get(key, {})
            if not isinstance(field_data, dict):
                original = str(field_data) if field_data else ""
            else:
                original = field_data.get("value", "")

            if new_value != original:
                has_changes = True
                if product_idx not in self.edit_state:
                    self.edit_state[product_idx] = {}
                self.edit_state[product_idx][key] = new_value

        if has_changes:
            # Recalculate confidence
            self._recalc_confidence(product_idx)
            self.product_list.update_row(product_idx)
            self.field_editor.populate(product, product_idx, self.edit_state)
            self.status_var.set(f"已保存产品 #{product_idx+1} 的修改")
        else:
            self.status_var.set("未检测到修改")

    def _on_field_navigate(self, direction: int):
        """Navigate to next/previous product."""
        if direction > 0:
            self.product_list.select_next()
        else:
            self.product_list.select_prev()

    def _recalc_confidence(self, product_idx: int):
        """Recalculate confidence_avg for a product after manual edits."""
        product = self.products[product_idx]
        edit = self.edit_state.get(product_idx, {})

        total_conf = 0.0
        field_count = 0
        for field_def in FIELD_DEFINITIONS:
            key = field_def["key"]
            field_data = product.get(key, {})
            if not isinstance(field_data, dict):
                conf = 0.0
            else:
                conf = field_data.get("confidence", 0.0)

            # Manual edits get 1.0 confidence
            if key in edit:
                conf = 1.0

            if conf > 0:
                total_conf += conf
                field_count += 1

        product["confidence_avg"] = round(total_conf / max(field_count, 1), 2)

    # ─── PDF Navigation ───

    def _nav_first_page(self):
        self.pdf_renderer.go_to_page(0)
        self._update_page_label()
        self._redraw_bboxes()

    def _nav_prev_page(self):
        if self.pdf_renderer.current_page > 0:
            self.pdf_renderer.prev_page()
            self._update_page_label()
            self._redraw_bboxes()

    def _nav_next_page(self):
        if self.pdf_renderer.current_page < self.pdf_renderer.page_count - 1:
            self.pdf_renderer.next_page()
            self._update_page_label()
            self._redraw_bboxes()

    def _nav_last_page(self):
        self.pdf_renderer.go_to_page(self.pdf_renderer.page_count - 1)
        self._update_page_label()
        self._redraw_bboxes()

    def _update_page_label(self):
        r = self.pdf_renderer
        self.page_label.config(text=f"第 {r.current_page+1}/{r.page_count} 页")
        # Update product list filter to current page
        # (optional: auto-filter)

    def _redraw_bboxes(self, product_idx: int = None):
        """Redraw bboxes on current PDF page (only selected product if given)."""
        if product_idx is not None and self.pdf_renderer.doc:
            self.bbox_overlay.show_single(
                product_idx, self.products,
                self.pdf_renderer.current_page, self.pdf_renderer.zoom,
            )
        else:
            self.bbox_overlay.clear()

    def _on_zoom_change(self, event=None):
        """Handle zoom level change."""
        zoom_str = self.zoom_var.get()
        if zoom_str == "适应宽度":
            self.pdf_renderer.zoom_fit_width()
        else:
            try:
                zoom = float(zoom_str.replace("%", "")) / 100
                self.pdf_renderer.set_zoom(zoom)
            except ValueError:
                pass
        # Re-show selected product's bbox at new zoom
        sel_idx = self.product_list.get_selected_index()
        self._redraw_bboxes(sel_idx)

    # ─── Export ───

    def _merge_products_with_edits(self) -> list:
        """Merge edit_state into products and return final list."""
        merged = []
        for idx, p in enumerate(self.products):
            product_copy = json.loads(json.dumps(p))  # deep copy
            if idx in self.edit_state:
                for key, new_value in self.edit_state[idx].items():
                    if key in [fd["key"] for fd in FIELD_DEFINITIONS]:
                        product_copy[key] = {
                            "value": new_value,
                            "confidence": 1.0,
                            "method": "manual",
                        }
                    elif key == "_product_image":
                        # Store manual image path
                        if "_matched_images" not in product_copy:
                            product_copy["_matched_images"] = []
                        product_copy["_matched_images"].insert(0, {
                            "xref": -1,
                            "filename": os.path.basename(new_value),
                            "bbox": [0, 0, 0, 0],
                            "confidence": 1.0,
                            "method": "manual_selection",
                        })
                product_copy["_edited"] = True
                # Recalculate confidence
                total_conf = 0.0
                field_count = 0
                for fd in FIELD_DEFINITIONS:
                    k = fd["key"]
                    fc = product_copy.get(k, {})
                    if isinstance(fc, dict) and fc.get("confidence", 0) > 0:
                        total_conf += fc["confidence"]
                        field_count += 1
                product_copy["confidence_avg"] = round(total_conf / max(field_count, 1), 2)
            merged.append(product_copy)
        return merged

    # ─── Session Save/Load ───

    SESSION_VERSION = "1.0"

    def _save_session(self):
        """Save current editing session to a file for later resume."""
        if not self.products:
            messagebox.showwarning("无数据", "没有可保存的项目数据")
            return

        filepath = filedialog.asksaveasfilename(
            title="保存项目",
            defaultextension=".json",
            filetypes=[("项目文件 (*.json)", "*.json"), ("All Files", "*.*")],
            initialfile=f"{Path(self.pdf_path).stem}_project.json" if self.pdf_path else "project.json",
        )
        if not filepath:
            return

        try:
            # Convert edit_state keys from int to str for JSON
            edit_state_str = {str(k): v for k, v in self.edit_state.items()}

            # Serialize all_matches (keys are page numbers as strings)
            all_matches_serializable = {}
            for page_key, matches in (self.all_matches or {}).items():
                all_matches_serializable[str(page_key)] = matches

            session = {
                "session_version": self.SESSION_VERSION,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_pdf": self.pdf_path,
                "products": self.products,
                "edit_state": edit_state_str,
                "all_matches": all_matches_serializable,
                "images_dir": self.images_dir,
                "output_dir": self.output_dir,
                "current_page": self.pdf_renderer.current_page if self.pdf_renderer else 0,
                "selected_product_idx": self.product_list.get_selected_index(),
                "current_zoom": self.pdf_renderer.zoom if self.pdf_renderer else 1.0,
            }

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(session, f, ensure_ascii=False, indent=2)

            edited = len(self.edit_state)
            self.status_var.set(f"项目已保存: {filepath} | {len(self.products)}产品, {edited}已修改")
            messagebox.showinfo(
                "保存成功",
                f"项目已保存到:\n{filepath}\n\n"
                f"产品数: {len(self.products)}\n"
                f"已编辑: {edited}\n"
                f"当前页码: {session['current_page'] + 1}",
            )
        except Exception as e:
            messagebox.showerror("保存失败", f"无法保存项目:\n{e}")

    def _load_session(self):
        """Load a previously saved editing session."""
        filepath = filedialog.askopenfilename(
            title="加载项目",
            filetypes=[("项目文件 (*.json)", "*.json"), ("All Files", "*.*")],
        )
        if not filepath:
            return

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                session = json.load(f)

            # Validate
            version = session.get("session_version", "unknown")
            if version != self.SESSION_VERSION:
                if not messagebox.askyesno(
                    "版本不匹配",
                    f"项目文件版本为 {version}，当前版本为 {self.SESSION_VERSION}。\n"
                    f"尝试加载可能存在兼容性问题，是否继续？",
                ):
                    return

            # Restore state
            self.pdf_path = session.get("source_pdf")
            self.products = session.get("products", [])
            # Convert edit_state keys back from str to int
            self.edit_state = {
                int(k): v for k, v in session.get("edit_state", {}).items()
            }
            self.all_matches = session.get("all_matches", {})
            self.images_dir = session.get("images_dir", "")
            self.output_dir = session.get("output_dir", "")
            saved_page = session.get("current_page", 0)
            saved_product_idx = session.get("selected_product_idx")
            saved_zoom = session.get("current_zoom", 1.0)

            # Open PDF for rendering
            pdf_loaded = False
            if self.pdf_path and os.path.isfile(self.pdf_path) and fitz:
                self.pdf_renderer.open_pdf(self.pdf_path)
                self.pdf_renderer.set_zoom(saved_zoom)
                self.pdf_renderer.go_to_page(saved_page)
                self._update_page_label()
                pdf_loaded = True
                self.file_label.config(text=f"{os.path.basename(self.pdf_path)}")
            elif self.pdf_path and not os.path.isfile(self.pdf_path):
                self.file_label.config(
                    text=f"⚠ PDF未找到: {os.path.basename(self.pdf_path)}"
                )
            else:
                self.file_label.config(text="⚠ 未加载PDF")

            # Update UI
            self.field_editor.set_images_dir(self.images_dir or "")
            self.product_list.load_products(self.products, self.edit_state)

            # Select saved product (or first)
            if saved_product_idx is not None and saved_product_idx < len(self.products):
                self.product_list.select_product(saved_product_idx)
            elif self.products:
                self.product_list.select_product(0)

            edited = len(self.edit_state)
            status_parts = [f"项目已加载: {os.path.basename(filepath)}"]
            status_parts.append(f"{len(self.products)}产品")
            if edited:
                status_parts.append(f"{edited}已修改")
            if not pdf_loaded:
                status_parts.append("(PDF未加载)")
            self.status_var.set(" | ".join(status_parts))

        except json.JSONDecodeError as e:
            messagebox.showerror("加载失败", f"项目文件格式错误:\n{e}")
        except Exception as e:
            messagebox.showerror("加载失败", f"无法加载项目:\n{e}")

    def _on_export_json(self):
        """Export merged products to JSON."""
        if not self.products:
            messagebox.showwarning("无数据", "没有可导出的产品数据")
            return

        filepath = filedialog.asksaveasfilename(
            title="导出JSON",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")],
            initialfile=f"{Path(self.pdf_path).stem}_corrected.json" if self.pdf_path else "products_corrected.json",
        )
        if not filepath:
            return

        try:
            merged = self._merge_products_with_edits()
            output = {
                "source_file": Path(self.pdf_path).stem if self.pdf_path else "unknown",
                "total_products": len(merged),
                "products": merged,
                "edited_count": len(self.edit_state),
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)

            edited = len(self.edit_state)
            self.status_var.set(f"已导出JSON: {filepath} | {len(merged)}产品, {edited}已修改")
            messagebox.showinfo("导出成功", f"已导出 {len(merged)} 个产品\n已修改: {edited} 个\n\n{filepath}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _on_export_excel(self):
        """Export merged products to Excel."""
        if not self.products:
            messagebox.showwarning("无数据", "没有可导出的产品数据")
            return

        filepath = filedialog.asksaveasfilename(
            title="导出Excel",
            defaultextension=".xlsx",
            filetypes=[("Excel Files", "*.xlsx")],
            initialfile=f"{Path(self.pdf_path).stem}_corrected.xlsx" if self.pdf_path else "products_corrected.xlsx",
        )
        if not filepath:
            return

        try:
            merged = self._merge_products_with_edits()
            step5_export_to_excel(merged, self.all_matches, self.images_dir or "", filepath, image_mode="embed")
            edited = len(self.edit_state)
            self.status_var.set(f"已导出Excel: {filepath} | {len(merged)}产品, {edited}已修改")
            messagebox.showinfo("导出成功", f"已导出 {len(merged)} 个产品\n已修改: {edited} 个\n\n{filepath}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _on_export_both(self):
        """Export to both JSON and Excel."""
        self._on_export_json()
        self._on_export_excel()

    # ─── Help ───

    def _on_field_config(self):
        """Open the global field configuration dialog (with template support)."""
        templates, active = load_templates()
        dialog = FieldConfigDialog(self.root, FIELD_DEFINITIONS, templates, active)
        if dialog.result is None:
            return  # Cancelled

        new_definitions, templates_to_save, active_name = dialog.result

        # Update global FIELD_DEFINITIONS in-place
        FIELD_DEFINITIONS[:] = new_definitions

        # Save templates
        if save_templates(templates_to_save, active_name):
            self.status_var.set(f"字段配置已保存 (模板: {active_name})")
        else:
            self.status_var.set("字段配置已更新 (未持久化)")

        # Rebuild the field editor UI
        self.field_editor.rebuild_fields()
        self.field_editor.set_callbacks(
            on_save=self._on_field_save,
            on_navigate=self._on_field_navigate,
            on_re_extract=self._on_field_re_extract,
            on_re_select_image=self._on_re_select_image,
        )

        # Re-populate current product if any
        sel_idx = self.product_list.get_selected_index()
        if sel_idx is not None and sel_idx < len(self.products):
            self.field_editor.populate(self.products[sel_idx], sel_idx, self.edit_state)
        else:
            self.field_editor.clear()

        # Refresh product list (confidence recalc uses new field defs)
        self.product_list.load_products(self.products, self.edit_state)
        if sel_idx is not None:
            self.product_list.select_product(sel_idx)

        # Force UI update
        self.field_editor.update_idletasks()

    def _on_about(self):
        messagebox.showinfo(
            "关于",
            "展会目录PDF自动提取工具 - MVP测试版\n\n"
            "功能:\n"
            "• 自动提取汽配展会目录PDF中的产品信息\n"
            "• 人工校验和修正提取结果\n"
            "• 导出为JSON和Excel格式\n\n"
            "快捷键:\n"
            "  Ctrl+O: 打开PDF  |  Ctrl+S: 导出JSON\n"
            "  Ctrl+E: 导出Excel |  Ctrl+N/P: 上/下条产品\n"
            "  Ctrl+R: 重新识别 |  Ctrl+Shift+S: 保存项目\n"
            "  Ctrl+Shift+O: 加载项目 |  Esc: 取消框选\n\n"
            "© 2026 MVP Technical Validation"
        )

    # ─── Run ───

    def run(self):
        """Start the main event loop."""
        self.root.mainloop()

    def quit(self):
        """Clean up and quit."""
        self.pdf_renderer.close()
        if self._pipeline_after_id:
            self.root.after_cancel(self._pipeline_after_id)
        self.root.quit()


# ═════════════════════════════════════════════════════
#  Entry Point
# ═════════════════════════════════════════════════════

def main():
    app = MainApplication()
    app.run()


if __name__ == "__main__":
    main()
