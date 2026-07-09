"""
可乐口播 v1.5 - 混剪 Tab UI（v3 重写 - 固定布局 + 滚动）
========================================================

v3 改动：
1. 去掉 PanedWindow 拖动条，左栏固定 340px，右栏自适应
2. 整个 Tab 用 Canvas + Scrollbar 滚动
3. 插入素材去掉横竖屏/分辨率（这些是全局输出设置）
4. 默认值：留空不限✅、随机🎲、无声🔇、替换主材、竖屏、移到子文件夹
5. 字段全中文 label
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import json
import threading
import subprocess
import shutil
import random
import re
from pathlib import Path

# =================================================================
# 配置路径
# =================================================================
MATERIAL_LIBRARY_FILE = "material_library.json"
LOG_DIR = Path.home() / ".cola_logs"
LOG_DIR.mkdir(exist_ok=True)


# =================================================================
# 字段常量
# =================================================================
LAYOUT_OPTIONS = [
    ("replaced",         "替换主材画面（铺满）"),
    ("pip_center",       "画中画 居中"),
    ("pip_top_left",     "画中画 左上角"),
    ("pip_top_right",    "画中画 右上角"),
    ("pip_bottom_left",  "画中画 左下角"),
    ("pip_bottom_right", "画中画 右下角"),
]
LAYOUT_DISPLAY = {k: v for k, v in LAYOUT_OPTIONS}

CLEANUP_OPTIONS = [
    ("move", "移到子文件夹"),
    ("trash", "删除到回收站"),
    ("keep", "不做处理"),
]
CLEANUP_DISPLAY = {k: v for k, v in CLEANUP_OPTIONS}

EXTRACT_OPTIONS = [
    ("random", "🎲 随机"),
    ("sequential", "顺序"),
]
EXTRACT_DISPLAY = {k: v for k, v in EXTRACT_OPTIONS}

ORIENTATION_OPTIONS = [
    ("portrait", "🎬 竖屏"),
    ("landscape", "横屏"),
]
ORIENTATION_DISPLAY = {k: v for k, v in ORIENTATION_OPTIONS}

RESOLUTION_OPTIONS = [
    ("720p", "📺 720P (高清)"),
    ("1080p", "📺 1080P (超清)"),
]
RESOLUTION_DISPLAY = {k: v for k, v in RESOLUTION_OPTIONS}


# =================================================================
# 工具函数
# =================================================================
def scan_videos(folder):
    """扫描文件夹内的视频文件"""
    if not folder or not os.path.isdir(folder):
        return []
    exts = (".mp4", ".mov", ".avi", ".mkv", ".webm")
    return sorted([
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(exts)
    ])


def time_to_seconds(time_str):
    """时间字符串（分:秒.百分秒）转秒"""
    if not time_str or time_str.strip() == "":
        return 0
    try:
        parts = time_str.split(":")
        if len(parts) != 2:
            return 0
        m = int(parts[0])
        s = float(parts[1])
        return m * 60 + s
    except Exception:
        return 0


def seconds_to_time(sec):
    """秒转分:秒.百分秒"""
    if sec is None or sec < 0:
        return "00:00.00"
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:05.2f}"


def parse_mmss(time_str):
    """解析分:秒.百分秒格式，3 段 spinbox"""
    if not time_str:
        return 0, 0, 0
    m = s = cs = 0
    try:
        if ":" in time_str:
            parts = time_str.split(":")
            m = int(parts[0])
            s_part = parts[1] if len(parts) > 1 else "0"
        else:
            s_part = time_str
        if "." in s_part:
            s_str, cs_str = s_part.split(".")
            s = int(s_str)
            cs = int(cs_str.ljust(2, "0")[:2])
        else:
            s = int(s_part)
    except Exception:
        pass
    return m, s, cs


def make_mmss(m, s, cs):
    """3 段 spinbox 值组合成字符串"""
    return f"{m:02d}:{s:02d}.{cs:02d}"


# =================================================================
# 主类
# =================================================================
class MergeTabFrame(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        # ====== 外层 Canvas + Scrollbar：整个 Tab 内容超出窗口高度时可上下滑 ======
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0)
        self.rowconfigure(0, weight=1)

        self._outer_canvas = tk.Canvas(self, highlightthickness=0)
        self._outer_canvas.grid(row=0, column=0, sticky="nsew")

        self._outer_scroll = ttk.Scrollbar(self, orient="vertical", command=self._outer_canvas.yview)
        self._outer_scroll.grid(row=0, column=1, sticky="ns")
        self._outer_canvas.configure(yscrollcommand=self._outer_scroll.set)

        # body frame 嵌到 canvas
        self.body = ttk.Frame(self._outer_canvas)
        self._body_window = self._outer_canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self._outer_canvas.configure(scrollregion=self._outer_canvas.bbox("all")))

        # body 宽度跟 canvas 一致，高度由 pack 累积自然撑高（超出 canvas 时 scrollbar 自动出）
        def _on_outer_configure(event):
            self._outer_canvas.itemconfig(self._body_window, width=event.width)
        self._outer_canvas.bind("<Configure>", _on_outer_configure)

        # 整个 Tab 触摸板/滚轮滚动
        def _on_outer_mw(event):
            if event.num == 4:
                delta = -1
            elif event.num == 5:
                delta = 1
            else:
                delta = -1 * event.delta
            self._outer_canvas.yview_scroll(int(delta), "units")
        self._outer_canvas.bind("<MouseWheel>", _on_outer_mw)
        self._outer_canvas.bind("<Button-4>", _on_outer_mw)
        self._outer_canvas.bind("<Button-5>", _on_outer_mw)

        # 变量
        self.segments = []  # 插入点列表
        self.main_video_dir = tk.StringVar(value="")
        self.output_dir = tk.StringVar(value="")
        self.output_suffix = tk.StringVar(value="_混剪")
        self.main_video_cleanup = tk.StringVar(value=CLEANUP_DISPLAY.get("keep", "不做处理"))
        self.orientation = tk.StringVar(value=ORIENTATION_DISPLAY.get("portrait", "📬 竖屏"))
        self.resolution = tk.StringVar(value=RESOLUTION_DISPLAY.get("1080p", "📺 1080P (超清)"))  # v2.10.14 默认改 1080P

        # 默认插入点
        self._default_segment = {
            "start_time": "00:00.00",
            "end_time": "",
            "end_unlimited": True,
            "material_folder": "",
            "note": "",
            "extract_mode": "random",
            "mute_material": True,
            "layout": "replaced",
            "pip_size": 50,
            "cleanup_mode": "move",
            "cleanup_subdir": "used",
        }

        self._build_ui()
        self._load_config()
        self._refresh_segment_list()

        # 🔥 关键：self 必须 pack 自己充满 merge_frame
        self.pack(fill="both", expand=True)

    # -----------------------------------------------------------------
    # UI 构建
    # -----------------------------------------------------------------
    def _build_ui(self):
        # 整个 Tab 用 grid 三行：top 固定 / mid 至少 350px / bottom 固定
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=0)  # top 固定（跟内容走）
        self.body.rowconfigure(1, weight=1, minsize=350)  # mid 至少 350px
        self.body.rowconfigure(2, weight=0)  # bottom 固定

        # ======== 顶部：主视频 + 输出（直接 LabelFrame，去掉横向 canvas 避免按钮被藏） ========
        top = ttk.LabelFrame(self.body, text="📁 主视频 / 输出设置", padding=8)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        # 2 列网格布局，3 行 = 节省一半高度
        top.columnconfigure(0, weight=1, uniform="a")
        top.columnconfigure(1, weight=1, uniform="a")

        def _add_field(parent, r, c, label_text, widget_factory):
            ttk.Label(parent, text=label_text).grid(row=r, column=c*2, sticky="w", padx=(2, 4), pady=(2, 0))
            w = widget_factory()
            w.grid(row=r, column=c*2+1, sticky="ew", padx=(0, 6), pady=(2, 0))

        # 第 0 行：主视频文件夹 | 输出文件夹
        _add_field(top, 0, 0, "主视频文件夹:", lambda: (
            fr := ttk.Frame(top),
            ttk.Entry(fr, textvariable=self.main_video_dir).pack(side="left", fill="x", expand=True, padx=(0, 4)),
            ttk.Button(fr, text="📁 选择", command=self._choose_main_video_dir, width=6).pack(side="left"),
            fr
        )[-1])
        _add_field(top, 0, 1, "输出文件夹:", lambda: (
            fr := ttk.Frame(top),
            ttk.Entry(fr, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=(0, 4)),
            ttk.Button(fr, text="📁 选择", command=self._choose_output_dir, width=6).pack(side="left"),
            fr
        )[-1])

        # 第 1 行：输出后缀 | 主视频清理
        ttk.Label(top, text="输出后缀:").grid(row=1, column=0, sticky="w", padx=(2, 4), pady=(6, 0))
        ttk.Entry(top, textvariable=self.output_suffix, width=15).grid(row=1, column=1, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Label(top, text="主视频清理:").grid(row=1, column=2, sticky="w", padx=(2, 4), pady=(6, 0))
        ttk.Combobox(top, textvariable=self.main_video_cleanup, width=14,
                     values=[CLEANUP_DISPLAY[k] for k, _ in CLEANUP_OPTIONS], state="readonly"
        ).grid(row=1, column=3, sticky="ew", padx=(0, 6), pady=(6, 0))

        # 第 2 行：输出横竖屏 | 输出分辨率
        ttk.Label(top, text="输出横竖屏:").grid(row=2, column=0, sticky="w", padx=(2, 4), pady=(6, 0))
        ttk.Combobox(top, textvariable=self.orientation, width=15,
                     values=[ORIENTATION_DISPLAY[k] for k, _ in ORIENTATION_OPTIONS], state="readonly"
        ).grid(row=2, column=1, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Label(top, text="输出分辨率:").grid(row=2, column=2, sticky="w", padx=(2, 4), pady=(6, 0))
        res_row = ttk.Frame(top)
        res_row.grid(row=2, column=3, sticky="ew", padx=(0, 6), pady=(6, 0))
        ttk.Combobox(res_row, textvariable=self.resolution, width=12,
                     values=[RESOLUTION_DISPLAY[k] for k, _ in RESOLUTION_OPTIONS], state="readonly"
        ).pack(side="left")
        ttk.Label(res_row, text="按原比例", foreground="gray").pack(side="left", padx=4)

        # ======== 中间：插入点列表 + 编辑面板 ========
        mid = ttk.Frame(self.body)
        mid.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        mid.columnconfigure(0, weight=0)  # 左固定
        mid.columnconfigure(1, weight=1)  # 右扩展
        mid.rowconfigure(0, weight=1)

        # 左：插入点列表（固定 340 宽）
        left = ttk.LabelFrame(mid, text=f"📋 插入点列表（{len(self.segments)} 个）", padding=5)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 5))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(left)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        ttk.Button(toolbar, text="➕ 添加", command=self._on_add_segment, width=10).pack(side="left", padx=2)
        ttk.Button(toolbar, text="💾 保存", command=self._save_config, width=10).pack(side="left", padx=2)
        ttk.Button(toolbar, text="📂 加载", command=self._load_config, width=10).pack(side="left", padx=2)

        list_outer = ttk.Frame(left)
        list_outer.grid(row=1, column=0, sticky="nsew")
        list_outer.columnconfigure(0, weight=1)
        list_outer.rowconfigure(0, weight=1)

        self.list_canvas = tk.Canvas(list_outer, width=320, highlightthickness=0, bg="#fafafa")
        list_scroll = ttk.Scrollbar(list_outer, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=list_scroll.set)
        self.list_canvas.grid(row=0, column=0, sticky="nsew")
        list_scroll.grid(row=0, column=1, sticky="ns")

        self.list_inner = ttk.Frame(self.list_canvas)
        self.list_canvas.create_window((0, 0), window=self.list_inner, anchor="nw", width=320)
        self.list_inner.bind("<Configure>", lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))

        def _on_mousewheel(event):
            if event.num == 4:
                delta = -1
            elif event.num == 5:
                delta = 1
            else:
                delta = -1 * event.delta
            self.list_canvas.yview_scroll(int(delta), "units")
        self.list_canvas.bind("<MouseWheel>", _on_mousewheel)
        self.list_canvas.bind("<Button-4>", _on_mousewheel)
        self.list_canvas.bind("<Button-5>", _on_mousewheel)

        # 右：编辑面板
        right = ttk.LabelFrame(mid, text="📝 插入点编辑", padding=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        self._build_editor(right)

        # ======== 底部：开始按钮 + 日志 ========
        bottom = ttk.Frame(self.body)
        bottom.grid(row=2, column=0, sticky="ew", padx=10, pady=(5, 10))
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(1, weight=1)

        # 按钮行
        btn_frame = ttk.Frame(bottom)
        btn_frame.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)

        self.start_btn = tk.Button(
            btn_frame, text="🚀 开 始 混 剪", font=("Arial", 14, "bold"),
            bg="#4CAF50", fg="white", activebackground="#45a049",
            height=2, command=self._on_start
        )
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5))

        self.stop_btn = tk.Button(
            btn_frame, text="⏹ 停止", font=("Arial", 12),
            bg="#f44336", fg="white", activebackground="#da190b",
            height=2, command=self._on_stop, state="disabled"
        )
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        # 日志（高度 16 行 + 可扩展）
        log_frame = ttk.LabelFrame(bottom, text="📋 运行日志", padding=5)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame, height=16, wrap="word", state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", font=("Menlo", 10)
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def _build_editor(self, parent):
        """右：插入点编辑面板（每个字段独占一整行，label 上面 + 控件下面）"""
        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        # 编辑器变量
        self.editing_index = None
        self.editing = {}

        # ===== 标题行（标题 + 删除按钮）=====
        title_frame = ttk.Frame(body)
        title_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        title_frame.columnconfigure(0, weight=1)
        self.editor_title = ttk.Label(title_frame, text="（请选择左侧插入点或点击 ➕ 添加）", font=("Arial", 12, "bold"))
        self.editor_title.grid(row=0, column=0, sticky="w")
        ttk.Button(title_frame, text="🗑 删除", command=self._on_delete_current, width=10).grid(row=0, column=1, padx=(8, 0))

        # ===== ⏱️ 时间段 =====
        time_frame = ttk.LabelFrame(body, text="⏱️ 时间段", padding=8)
        time_frame.grid(row=1, column=0, sticky="ew", pady=5)
        time_frame.columnconfigure(0, weight=1)

        ttk.Label(time_frame, text="开始时间（分 : 秒 . 百分秒）").grid(row=0, column=0, sticky="w", pady=(2, 3))
        start_row = ttk.Frame(time_frame)
        start_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.editing["start_m"] = tk.IntVar(value=0)
        self.editing["start_s"] = tk.IntVar(value=0)
        self.editing["start_cs"] = tk.IntVar(value=0)
        ttk.Spinbox(start_row, from_=0, to=99, width=4, textvariable=self.editing["start_m"]).pack(side="left")
        ttk.Label(start_row, text="分").pack(side="left", padx=(2, 12))
        ttk.Spinbox(start_row, from_=0, to=59, width=4, textvariable=self.editing["start_s"]).pack(side="left")
        ttk.Label(start_row, text="秒").pack(side="left", padx=(2, 12))
        ttk.Spinbox(start_row, from_=0, to=99, width=4, textvariable=self.editing["start_cs"]).pack(side="left")
        ttk.Label(start_row, text="百分秒").pack(side="left", padx=(2, 0))

        ttk.Label(time_frame, text="结束时间（留空 = 不限）").grid(row=2, column=0, sticky="w", pady=(8, 3))
        end_row = ttk.Frame(time_frame)
        end_row.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self.editing["end_m"] = tk.IntVar(value=0)
        self.editing["end_s"] = tk.IntVar(value=0)
        self.editing["end_cs"] = tk.IntVar(value=0)
        ttk.Spinbox(end_row, from_=0, to=99, width=4, textvariable=self.editing["end_m"]).pack(side="left")
        ttk.Label(end_row, text="分").pack(side="left", padx=(2, 12))
        ttk.Spinbox(end_row, from_=0, to=59, width=4, textvariable=self.editing["end_s"]).pack(side="left")
        ttk.Label(end_row, text="秒").pack(side="left", padx=(2, 12))
        ttk.Spinbox(end_row, from_=0, to=99, width=4, textvariable=self.editing["end_cs"]).pack(side="left")
        ttk.Label(end_row, text="百分秒").pack(side="left", padx=(2, 0))

        self.editing["end_unlimited"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            time_frame, text="☑ 留空不限（结束时间不生效）",
            variable=self.editing["end_unlimited"]
        ).grid(row=4, column=0, sticky="w", pady=(4, 0))

        # ===== 📁 素材文件夹 =====
        mat_frame = ttk.LabelFrame(body, text="📁 素材文件夹", padding=8)
        mat_frame.grid(row=2, column=0, sticky="ew", pady=5)
        mat_frame.columnconfigure(0, weight=1)

        ttk.Label(mat_frame, text="📂 路径（必填，选好文件夹后素材数会自动显示）").grid(row=0, column=0, sticky="w", pady=(0, 3))
        path_row = ttk.Frame(mat_frame)
        path_row.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        path_row.columnconfigure(0, weight=1)
        self.editing["material_folder"] = tk.StringVar(value="")
        ttk.Entry(path_row, textvariable=self.editing["material_folder"]).grid(row=0, column=0, sticky="ew", padx=(0, 5), ipady=3)
        ttk.Button(path_row, text="📁 选择...", command=self._choose_material_folder, width=12).grid(row=0, column=1, ipady=3)

        self.material_count_label = ttk.Label(mat_frame, text="（未选择）", foreground="gray")
        self.material_count_label.grid(row=2, column=0, sticky="w", pady=(0, 6))

        ttk.Label(mat_frame, text="📝 备注（可选）").grid(row=3, column=0, sticky="w", pady=(4, 3))
        self.editing["note"] = tk.StringVar(value="")
        ttk.Entry(mat_frame, textvariable=self.editing["note"]).grid(row=4, column=0, sticky="ew", ipady=3)

        # ===== 🎲 抽取方式 =====
        ext_frame = ttk.LabelFrame(body, text="🎲 抽取方式", padding=8)
        ext_frame.grid(row=3, column=0, sticky="ew", pady=5)
        ext_frame.columnconfigure(0, weight=1)

        ttk.Label(ext_frame, text="抽取方式").grid(row=0, column=0, sticky="w", pady=(0, 3))
        radio_row = ttk.Frame(ext_frame)
        radio_row.grid(row=1, column=0, sticky="w", pady=(0, 8))
        self.editing["extract_mode"] = tk.StringVar(value=EXTRACT_DISPLAY.get("random", "🎲 随机"))
        for k, v in EXTRACT_OPTIONS:
            ttk.Radiobutton(radio_row, text=v, variable=self.editing["extract_mode"], value=k).pack(side="left", padx=(0, 15))

        self.editing["mute_material"] = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ext_frame, text="🔇 插入素材无声（不勾 = 保留素材原声与主视频混音）",
            variable=self.editing["mute_material"]
        ).grid(row=2, column=0, sticky="w", pady=(0, 0))

        # ===== 🖼 画面布局 =====
        layout_frame = ttk.LabelFrame(body, text="🖼 画面布局", padding=8)
        layout_frame.grid(row=4, column=0, sticky="ew", pady=5)
        layout_frame.columnconfigure(0, weight=1)

        ttk.Label(layout_frame, text="布局方式").grid(row=0, column=0, sticky="w", pady=(0, 3))
        self.editing["layout"] = tk.StringVar(value=LAYOUT_DISPLAY.get("replaced", "替换主材画面（铺满）"))
        ttk.Combobox(
            layout_frame, textvariable=self.editing["layout"],
            values=[v for _, v in LAYOUT_OPTIONS], state="readonly"
        ).grid(row=1, column=0, sticky="ew", pady=(0, 8), ipady=3)

        ttk.Label(layout_frame, text="PIP 大小（画中画时才生效）").grid(row=2, column=0, sticky="w", pady=(8, 3))
        pip_row = ttk.Frame(layout_frame)
        pip_row.grid(row=3, column=0, sticky="ew")
        pip_row.columnconfigure(0, weight=1)
        self.editing["pip_size"] = tk.IntVar(value=50)
        self.pip_scale = ttk.Scale(pip_row, from_=10, to=80, variable=self.editing["pip_size"], orient="horizontal")
        self.pip_scale.grid(row=0, column=0, sticky="ew")
        self.pip_size_label = ttk.Label(pip_row, text="50%", width=6, anchor="e")
        self.pip_size_label.grid(row=0, column=1, padx=(8, 0))

        def _update_pip_label(*args):
            self.pip_size_label.config(text=f"{self.editing['pip_size'].get()}%")
        self.editing["pip_size"].trace_add("write", _update_pip_label)

        # PIP 边距（仅画中画时生效，铺底主视频保留完整画布）
        ttk.Label(layout_frame, text="PIP 边距（距画布边缘的像素，画中画时才生效）").grid(row=4, column=0, sticky="w", pady=(8, 3))
        self.editing["pip_margin"] = tk.IntVar(value=20)
        ttk.Entry(layout_frame, textvariable=self.editing["pip_margin"], width=8).grid(row=5, column=0, sticky="w", ipady=3)

        def _update_pip_state(*args):
            current_layout_key = self._reverse_lookup(LAYOUT_DISPLAY, self.editing["layout"].get(), "replaced")
            if current_layout_key == "replaced":
                self.pip_scale.state(["disabled"])
                self.pip_size_label.config(foreground="gray")
                try:
                    self.editing["pip_margin"].set(0)
                except Exception:
                    pass
            else:
                self.pip_scale.state(["!disabled"])
                self.pip_size_label.config(foreground="black")
        self.editing["layout"].trace_add("write", _update_pip_state)
        _update_pip_state()

        # ===== 🗑 用过素材处理 =====
        cleanup_frame = ttk.LabelFrame(body, text="🗑 用过素材处理", padding=8)
        cleanup_frame.grid(row=5, column=0, sticky="ew", pady=5)
        cleanup_frame.columnconfigure(0, weight=1)

        ttk.Label(cleanup_frame, text="处理方式").grid(row=0, column=0, sticky="w", pady=(0, 3))
        self.editing["cleanup_mode"] = tk.StringVar(value=CLEANUP_DISPLAY.get("move", "移到子文件夹"))
        ttk.Combobox(
            cleanup_frame, textvariable=self.editing["cleanup_mode"],
            values=[v for _, v in CLEANUP_OPTIONS], state="readonly"
        ).grid(row=1, column=0, sticky="ew", pady=(0, 8), ipady=3)

        ttk.Label(cleanup_frame, text="子目录名（移动/复制模式时用）").grid(row=2, column=0, sticky="w", pady=(8, 3))
        self.editing["cleanup_subdir"] = tk.StringVar(value="used")
        ttk.Entry(cleanup_frame, textvariable=self.editing["cleanup_subdir"]).grid(row=3, column=0, sticky="ew", ipady=3)

        # ===== 操作按钮（一行两个：保存 + 取消）=====
        btn_frame = ttk.Frame(body)
        btn_frame.grid(row=6, column=0, sticky="ew", pady=(12, 8))
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)
        ttk.Button(btn_frame, text="✅ 保存到列表", command=self._on_save_segment).grid(
            row=0, column=0, sticky="ew", padx=(0, 5), ipady=10)
        ttk.Button(btn_frame, text="取消", command=self._on_cancel_edit).grid(
            row=0, column=1, sticky="ew", ipady=10)

        self._set_editor_state("normal")

    def _set_editor_state(self, state):
        """递归启用/禁用编辑面板里所有输入控件"""
        if not hasattr(self, 'editor_title') or self.editor_title is None:
            return
        try:
            body = self.editor_title.master.master
        except Exception:
            return

        def walk(widget):
            for cls in (ttk.Entry, ttk.Spinbox, ttk.Checkbutton, ttk.Radiobutton, ttk.Button, ttk.Scale):
                try:
                    if isinstance(widget, cls):
                        if isinstance(widget, ttk.Combobox):
                            widget.configure(state="disabled" if state == "disabled" else "readonly")
                        else:
                            widget.configure(state=state)
                except Exception:
                    pass
            for child in widget.winfo_children():
                walk(child)
        walk(body)
    def _refresh_segment_list(self):
        for w in self.list_inner.winfo_children():
            w.destroy()

        # 更新标题
        for child in self.winfo_children():
            pass

        for i, seg in enumerate(self.segments):
            self._build_segment_card(i, seg)

        if not self.segments:
            ttk.Label(self.list_inner, text="（暂无插入点）", foreground="gray").pack(pady=20)

        # 更新标题数字
        for w in self.winfo_children():
            if isinstance(w, ttk.LabelFrame) and "插入点列表" in w.cget("text"):
                w.configure(text=f"📋 插入点列表（{len(self.segments)} 个）")
                break

    def _build_segment_card(self, idx, seg):
        card = tk.Frame(self.list_inner, bd=1, relief="solid", bg="white", padx=8, pady=6)
        card.pack(fill="x", pady=4, padx=2)

        # 顶部行
        top = tk.Frame(card, bg="white")
        top.pack(fill="x")

        time_str = seg.get("start_time", "00:00.00")
        end_unlimited = seg.get("end_unlimited", True)
        end_str = "不限" if end_unlimited else seg.get("end_time", "00:00.00")
        layout_disp = LAYOUT_DISPLAY.get(seg.get("layout", "replaced"), "替换主材画面")
        extract_disp = EXTRACT_DISPLAY.get(seg.get("extract_mode", "random"), "随机")
        cleanup_disp = CLEANUP_DISPLAY.get(seg.get("cleanup_mode", "move"), "移到子文件夹")
        mute_disp = "无声" if seg.get("mute_material", True) else "有声"
        note = seg.get("note", "")

        # 标题
        title = f"插入点 {idx + 1}"
        if note:
            title += f"  · {note}"
        tk.Label(top, text=title, font=("Arial", 11, "bold"), bg="white", fg="#1976D2").pack(side="left")

        # 时段（蓝色等宽）
        time_label = tk.Label(
            top, text=f"{time_str} → {end_str}",
            font=("Menlo", 10, "bold"), bg="white", fg="#FF5722"
        )
        time_label.pack(side="right")

        # 详情
        detail_text = f"📁 {os.path.basename(seg.get('material_folder', '')) or '（未选）'}\n"
        detail_text += f"🖼 {layout_disp}\n"
        detail_text += f"🔊 {mute_disp} · 🎲 {extract_disp}\n"
        detail_text += f"🗑 {cleanup_disp}"
        tk.Label(card, text=detail_text, font=("Arial", 9), bg="white", fg="#555", justify="left", anchor="w").pack(fill="x", pady=(4, 0))

        # 事件绑定
        def on_click(e, i=idx):
            self._on_select_segment(i)
        def on_double_click(e, i=idx):
            self._load_segment_to_editor(i)

        for w in [card, top, time_label] + list(card.winfo_children()):
            try:
                w.bind("<Button-1>", on_click)
                w.bind("<Double-Button-1>", on_double_click)
            except Exception:
                pass

    # -----------------------------------------------------------------
    # 编辑器交互
    # -----------------------------------------------------------------
    def _on_add_segment(self):
        self.editing_index = None
        self.editor_title.config(text="➕ 新建插入点")
        self._reset_editor()
        self._set_editor_state("normal")
        self._update_material_count()

    def _on_select_segment(self, idx):
        self.editing_index = idx
        seg = self.segments[idx]
        self.editor_title.config(text=f"📝 插入点 {idx + 1}（已选）")
        self._load_segment_to_editor(idx)
        self._set_editor_state("normal")

    def _load_segment_to_editor(self, idx):
        seg = self.segments[idx]
        self.editing_index = idx
        self.editor_title.config(text=f"📝 编辑插入点 {idx + 1}")

        sm, ss, scs = parse_mmss(seg.get("start_time", "00:00.00"))
        em, es, ecs = parse_mmss(seg.get("end_time", "00:00.00"))

        self.editing["start_m"].set(sm)
        self.editing["start_s"].set(ss)
        self.editing["start_cs"].set(scs)
        self.editing["end_m"].set(em)
        self.editing["end_s"].set(es)
        self.editing["end_cs"].set(ecs)
        self.editing["end_unlimited"].set(seg.get("end_unlimited", True))
        self.editing["material_folder"].set(seg.get("material_folder", ""))
        self.editing["note"].set(seg.get("note", ""))
        self.editing["extract_mode"].set(seg.get("extract_mode", "random"))
        self.editing["mute_material"].set(seg.get("mute_material", True))
        self.editing["layout"].set(LAYOUT_DISPLAY.get(seg.get("layout", "replaced"), "替换主材画面（黑边填）"))
        self.editing["pip_size"].set(seg.get("pip_size", 50))
        self.editing["cleanup_mode"].set(CLEANUP_DISPLAY.get(seg.get("cleanup_mode", "move"), "移到子文件夹"))
        self.editing["cleanup_subdir"].set(seg.get("cleanup_subdir", "used"))

        self._update_material_count()

    def _reset_editor(self):
        sm, ss, scs = parse_mmss("00:00.00")
        self.editing["start_m"].set(0)
        self.editing["start_s"].set(0)
        self.editing["start_cs"].set(0)
        self.editing["end_m"].set(0)
        self.editing["end_s"].set(0)
        self.editing["end_cs"].set(0)
        self.editing["end_unlimited"].set(True)
        self.editing["material_folder"].set("")
        self.editing["note"].set("")
        self.editing["extract_mode"].set(EXTRACT_DISPLAY.get("random", "🎲 随机"))
        self.editing["mute_material"].set(True)
        self.editing["layout"].set(LAYOUT_DISPLAY.get("replaced", "替换主材画面（铺满）"))
        self.editing["pip_size"].set(50)
        self.editing["cleanup_mode"].set("移到子文件夹")
        self.editing["cleanup_subdir"].set("used")
        self._update_material_count()

    def _on_save_segment(self):
        seg = {
            "start_time": make_mmss(
                self.editing["start_m"].get(),
                self.editing["start_s"].get(),
                self.editing["start_cs"].get()
            ),
            "end_time": make_mmss(
                self.editing["end_m"].get(),
                self.editing["end_s"].get(),
                self.editing["end_cs"].get()
            ),
            "end_unlimited": self.editing["end_unlimited"].get(),
            "material_folder": self.editing["material_folder"].get(),
            "note": self.editing["note"].get(),
            "extract_mode": self.editing["extract_mode"].get(),
            "mute_material": self.editing["mute_material"].get(),
            "layout": self._reverse_lookup(LAYOUT_DISPLAY, self.editing["layout"].get(), "replaced"),
            "pip_size": self.editing["pip_size"].get(),
            "pip_margin": self.editing["pip_margin"].get(),
            "cleanup_mode": self._reverse_lookup(CLEANUP_DISPLAY, self.editing["cleanup_mode"].get(), "move"),
            "cleanup_subdir": self.editing["cleanup_subdir"].get(),
        }

        if self.editing_index is None:
            self.segments.append(seg)
        else:
            self.segments[self.editing_index] = seg

        self._refresh_segment_list()
        self._log(f"✅ 插入点已保存：{seg['start_time']} → {'不限' if seg['end_unlimited'] else seg['end_time']}")

    def _on_cancel_edit(self):
        self.editing_index = None
        self.editor_title.config(text="（已取消）")
        self._set_editor_state("disabled")

    def _on_delete_current(self):
        if self.editing_index is None:
            messagebox.showinfo("提示", "请先在左侧选中一个插入点")
            return
        if messagebox.askyesno("确认", f"删除插入点 {self.editing_index + 1}？"):
            del self.segments[self.editing_index]
            self.editing_index = None
            self.editor_title.config(text="（已删除）")
            self._reset_editor()
            self._set_editor_state("disabled")
            self._refresh_segment_list()

    def _reverse_lookup(self, mapping, value, default):
        """v1.47：兼容两种输入
        - 传 key（如 "move"）→ 直接返回 key
        - 传 value（如 "移到子文件夹"）→ 反查成 key
        - 都不匹配 → 返回 default
        这样不管调用方传哪种格式都正确，避免 ai_tab 推 key（"move"）却反查失败回退到 "keep"（不做处理）的 bug。
        """
        if value in mapping:                 # 优先：value 是 key
            return value
        for k, v in mapping.items():         # 兜底：value 是 display（中文）
            if v == value:
                return k
        return default

    # -----------------------------------------------------------------
    # 文件夹选择
    # -----------------------------------------------------------------
    def _choose_main_video_dir(self):
        path = filedialog.askdirectory(title="选择主视频文件夹")
        if path:
            self.main_video_dir.set(path)
            if not self.output_dir.get():
                self.output_dir.set(path)  # 默认输出到主视频文件夹

    def _choose_output_dir(self):
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            self.output_dir.set(path)

    def _choose_material_folder(self):
        path = filedialog.askdirectory(title="选择素材文件夹")
        if path:
            self.editing["material_folder"].set(path)
            self._update_material_count()

    def _update_material_count(self):
        folder = self.editing["material_folder"].get()
        if not folder or not os.path.isdir(folder):
            self.material_count_label.config(text="（未选择 / 路径无效）", foreground="gray")
            return
        videos = scan_videos(folder)
        if not videos:
            self.material_count_label.config(text="⚠ 0 个素材", foreground="red")
        else:
            self.material_count_label.config(text=f"✅ {len(videos)} 个素材", foreground="green")

    # -----------------------------------------------------------------
    # 配置保存/加载
    # -----------------------------------------------------------------
    def _save_config(self):
        config = {
            "main_video_dir": self.main_video_dir.get(),
            "output_dir": self.output_dir.get(),
            "output_suffix": self.output_suffix.get(),
            "main_video_cleanup": self.main_video_cleanup.get(),
            "orientation": self.orientation.get(),
            "resolution": self.resolution.get(),
            "segments": self.segments,
        }
        try:
            with open(MATERIAL_LIBRARY_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self._log(f"💾 配置已保存到 {MATERIAL_LIBRARY_FILE}")
            messagebox.showinfo("成功", "配置已保存")
        except Exception as e:
            messagebox.showerror("失败", f"保存失败：{e}")

    def _trace_output_dir(self):
        """v1.44：tab5 output_dir 变化时同步到 config.yaml defaults.output_dir"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            value = self.output_dir.get().strip()
            if not value:
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["output_dir"] = value
            if hasattr(self.app, "output_dir_var"):
                try:
                    self.app.output_dir_var.set(value)
                except Exception:
                    pass
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception:
                    pass
        self.output_dir.trace_add("write", _on_change)

    def _load_config(self):
        if not os.path.exists(MATERIAL_LIBRARY_FILE):
            self._log("ℹ️ 暂无配置文件")
            return
        try:
            with open(MATERIAL_LIBRARY_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            self.main_video_dir.set(config.get("main_video_dir", ""))
            # v1.44：tab5 output_dir 没保存时 fallback 到 config.yaml defaults.output_dir（和成品 Tab 同步）
            output_dir = config.get("output_dir", "")
            if not output_dir and self.app is not None and hasattr(self.app, "config"):
                output_dir = self.app.config.get("defaults", {}).get("output_dir", "")
            self.output_dir.set(output_dir)
            # v1.44：trace output_dir 变化时同步到 config.yaml（让 Tab5 / Tab6 / 成品 Tab 共享）
            self._trace_output_dir()
            self.output_suffix.set(config.get("output_suffix", "_混剪"))
            cleanup = config.get("main_video_cleanup", "keep")
            self.main_video_cleanup.set(CLEANUP_DISPLAY.get(cleanup, "不做处理"))
            orientation = config.get("orientation", "portrait")
            self.orientation.set(ORIENTATION_DISPLAY.get(orientation, "竖屏"))
            resolution = config.get("resolution", "720p")
            self.resolution.set(RESOLUTION_DISPLAY.get(resolution, "📺 720P (高清)"))
            self.segments = config.get("segments", [])
            self._refresh_segment_list()
            self._log(f"📂 配置已加载：{len(self.segments)} 个插入点")
        except Exception as e:
            messagebox.showerror("失败", f"加载失败：{e}")

    # -----------------------------------------------------------------
    # 开始混剪
    # -----------------------------------------------------------------
    def _on_start(self):
        # v1.34：auto 模式下跳过 messagebox 早返回（避免弹窗被忽略导致 _ai_batch_running 卡死）
        in_auto_mode = bool(getattr(self, "_ai_auto_callback", None))

        # v1.44 跨 Tab 互斥：AI 自动流水线里跳过检查（auto 模式专门让 AI 控制）
        if not in_auto_mode and self.app is not None and hasattr(self.app, "task_queue") and self.app.task_queue.is_busy():
            current = self.app.task_queue.current_label()
            qsize = self.app.task_queue.queue_size()
            if not messagebox.askyesno(
                "任务互斥",
                f"⚠️ {current} 正在跑\n\n"
                f"📋 当前排队：{qsize} 个任务\n\n"
                f"选「是」加入排队\n选「否」取消本次运行"
            ):
                return
            display = f"Tab 5 手动混剪（{len(self.segments)} 个插入点）"
            self._log(f"📋 已加入排队（位置 {qsize + 1}）：{display}")
            self.app.task_queue.request(
                "Tab 5", display,
                callback=self._do_run_merge,
                on_done=None,
            )
            return

        if not self.segments:
            self._log(f"[AI→Tab5] ⚠️ 调度未启动：segments 为空（共 0 个插入点）{'[auto 模式]' if in_auto_mode else ''}")
            if not in_auto_mode:
                messagebox.showwarning("提示", "请先添加至少一个插入点")
            return
        if not self.main_video_dir.get() or not os.path.isdir(self.main_video_dir.get()):
            main_dir = self.main_video_dir.get()
            self._log(f"[AI→Tab5] ⚠️ 调度未启动：main_video_dir 无效 ({main_dir!r}){'[auto 模式]' if in_auto_mode else ''}")
            if not in_auto_mode:
                messagebox.showwarning("提示", "请先选择主视频文件夹")
            return

        self.start_btn.config(state="disabled", text="⏳ 混剪中...")
        self.stop_btn.config(state="normal")

        try:
            from merge_engine import MixScheduler, MixConfig, InsertPoint, OutputSettings
        except ImportError as e:
            self.start_btn.config(state="normal", text="🚀 开 始 混 剪")
            self.stop_btn.config(state="disabled")
            messagebox.showerror("错误", f"加载引擎失败：{e}")
            self._log(f"❌ 引擎导入失败：{e}")
            return

        def _mmss_to_sec(mmss):
            """'MM:SS.CS' → 秒（失败回 0）"""
            try:
                m, rest = mmss.split(":")
                s, cs = rest.split(".")
                return int(m) * 60 + int(s) + int(cs) / 100.0
            except Exception:
                return 0.0

        # AI exec_config 覆盖（v1.14 Stage 3）：如果 AI Tab 推过来的、且该段 note 仍带 [AI] 标记
        # （即用户没在 Tab 5 编辑器改过），把 mute/layout/pip/cleanup 强制用 exec_config
        # v1.47：ai_tab 现在推中文 display 名（如"移到子文件夹"/"替换主材画面（铺满）"），
        # 需要反查成 key（move/replaced），否则后端匹配不到
        ai_cfg = getattr(self, "_ai_exec_config_override", None)
        ai_overridden = 0
        if ai_cfg:
            # 提前反查中文→key（如果还是 key 形式，反查函数也能用 default 兜底）
            ai_layout_key = self._reverse_lookup(LAYOUT_DISPLAY, ai_cfg.get("layout", ""), "replaced")
            ai_cleanup_key = self._reverse_lookup(CLEANUP_DISPLAY, ai_cfg.get("cleanup_mode", ""), "move")
            for s in self.segments:
                if "[AI]" not in s.get("note", ""):
                    continue
                if "mute_material" in ai_cfg:
                    s["mute_material"] = bool(ai_cfg["mute_material"])
                if "layout" in ai_cfg:
                    s["layout"] = ai_layout_key
                if "pip_size" in ai_cfg:
                    s["pip_size"] = int(ai_cfg["pip_size"])
                if "pip_margin" in ai_cfg:
                    s["pip_margin"] = int(ai_cfg["pip_margin"])
                if "cleanup_mode" in ai_cfg:
                    s["cleanup_mode"] = ai_cleanup_key
                ai_overridden += 1
            if ai_overridden:
                self._log(f"🤖 AI 配置覆盖 {ai_overridden} 个插入点：layout/mute/pip/cleanup")
                self._log(f"   配置：{ai_cfg}")

        # self.segments (dict 列表) → List[InsertPoint]
        insert_points = []
        for s in self.segments:
            ip = InsertPoint(
                startTime=_mmss_to_sec(s.get("start_time", "00:00.00")),
                endTime=0.0 if s.get("end_unlimited", True) else _mmss_to_sec(s.get("end_time", "00:00.00")),
                folder=s.get("material_folder", ""),
                cleanupMode=s.get("cleanup_mode", "move"),
                silent=bool(s.get("mute_material", True)),
                layout=s.get("layout", "replaced"),
                pipScale=int(s.get("pip_size", 50)) / 100.0,
                pipMargin=int(s.get("pip_margin", 20)),
                note=s.get("note", ""),
            )
            insert_points.append(ip)

        # v1.43：优先用 AI exec_config 推过来的 main_video_cleanup（tab6 选了什么就用什么）
        # tab5 自己的 UI 只在 AI 没推时生效（手动从 tab5 点混剪的场景）
        ai_cfg = getattr(self, "_ai_exec_config_override", None) or {}
        ai_main_cleanup = ai_cfg.get("main_video_cleanup")
        # v1.47：debug 写文件（不依赖 GUI 日志）
        try:
            import datetime as _dt
            ts = _dt.datetime.now().strftime("%H:%M:%S")
            with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [DBG-MERGE] _ai_exec_config_override = {ai_cfg!r}\n")
                f.write(f"[{ts}] [DBG-MERGE] ai_main_cleanup = {ai_main_cleanup!r}\n")
        except Exception:
            pass
        # v1.47：直接交给 _reverse_lookup 处理（它兼容 key 或 display 两种输入）
        if ai_main_cleanup:
            main_cleanup_key = self._reverse_lookup(CLEANUP_DISPLAY, ai_main_cleanup, "keep")
        else:
            main_cleanup_key = self._reverse_lookup(CLEANUP_DISPLAY, self.main_video_cleanup.get(), "keep")

        # v1.50：从 _ai_archive_dir 拿归档目录（auto_run 时存到这的）
        archive_dir = getattr(self, "_ai_archive_dir", "") or self.main_video_dir.get()
        # v2.10.14 修：OutputSettings.resolution 用 self.resolution（之前 hardcode 720p 永远输出 720p, 不管 UI 选什么）
        # 用 _reverse_lookup 兼容 display 中文/英文 key 两种输入
        resolution_key = self._reverse_lookup(RESOLUTION_DISPLAY, self.resolution.get(), "1080p")
        config = MixConfig(
            mainFolder=self.main_video_dir.get(),
            insertPoints=insert_points,
            outputFolder=self.output_dir.get() or self.main_video_dir.get(),
            outputSuffix=self.output_suffix.get() or "_混剪",
            outputSettings=OutputSettings(orientation="portrait", resolution=resolution_key),
            mainCleanupMode=main_cleanup_key,
            mainArchiveDir=archive_dir,  # v1.50：主视频归档目录
        )
        self._log(f"[debug merge_tab] main_cleanup_key = {main_cleanup_key!r} → MixConfig.mainCleanupMode = {config.mainCleanupMode!r}")  # v1.47 debug
        # v1.47：写文件 debug log
        try:
            import datetime as _dt
            ts = _dt.datetime.now().strftime("%H:%M:%S")
            with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [DBG-MERGE] main_cleanup_key = {main_cleanup_key!r} → MixConfig.mainCleanupMode = {config.mainCleanupMode!r}\n")
                f.write(f"[{ts}] [DBG-MERGE] CLEANUP_DISPLAY = {CLEANUP_DISPLAY!r}\n")
        except Exception:
            pass

        self.scheduler = MixScheduler(
            config,
            log_callback=self._log,
            on_finished=self._on_finished,
        )
        self.scheduler.start()

    def _on_finished(self, success):
        """scheduler 跑完/停止/异常时调（在 worker 线程）。回到主线程恢复 UI。
        v1.23：自动模式下传 dict 给回调，含 success/error/stopped。
        """
        def _reset():
            self.start_btn.config(state="normal", text="🚀 开 始 混 剪")
            self.stop_btn.config(state="disabled")
            stopped = False
            error_msg = ""
            if not success and self.scheduler and self.scheduler.was_stopped():
                self._log("⏹ 已停止")
                stopped = True
                error_msg = "用户手动停止"
            elif not success:
                exc = self.scheduler._exception if self.scheduler else None
                error_msg = f"{type(exc).__name__}: {exc}" if exc else "未知错误（无异常对象）"
                self._log(f"❌ 异常结束：{error_msg}")
            else:
                self._log("✅ 全部完成")

            # v1.21：自动模式下回调 ai_tab，触发流水线下一个
            cb = getattr(self, "_ai_auto_callback", None)
            tmp_dir = getattr(self, "_ai_auto_temp_dir", None)
            if cb:
                self._ai_auto_callback = None
                self._ai_auto_temp_dir = None
                # 清理临时目录（视频是 symlink，目录删了视频本身不受影响）
                if tmp_dir:
                    try:
                        import shutil
                        shutil.rmtree(tmp_dir, ignore_errors=True)
                    except Exception:
                        pass
                try:
                    # v1.23：传 dict 而不是 bool，便于 ai_tab 区分失败原因
                    cb({
                        "success": bool(success),
                        "error": error_msg,
                        "stopped": stopped,
                    })
                except Exception as e:
                    self._log(f"[AI→Tab5] ❌ 自动回调失败：{e}")

        # v1.44：释放全局任务队列（手动模式下）
        if not getattr(self, "_ai_auto_callback_origin", None):
            try:
                if self.app is not None and hasattr(self.app, "task_queue"):
                    self.app.task_queue.release()
            except Exception:
                pass

        self.after(0, _reset)

    def _poll_thread(self, thread):
        # 不再用轮询，scheduler.on_finished 回调
        pass

    def _do_run_merge(self):
        """v1.44：被 task_queue 调度的 callback"""
        self._on_start()

    def _on_stop(self):
        if hasattr(self, 'scheduler') and self.scheduler and self.scheduler.is_running():
            self.scheduler.stop()
            self._log("⏹ 用户请求停止...")

    # -----------------------------------------------------------------
    # 日志
    # -----------------------------------------------------------------
    def _log(self, msg):
        def _append():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.after(0, _append)

    # -----------------------------------------------------------------
    # v1.13: 接收 AI 智能 Tab 推送
    # v1.21: 增加 auto_run 自动执行模式（流水线）
    # -----------------------------------------------------------------
    def load_from_ai_tab(self, payload: dict):
        """
        从 AI 智能 Tab 接收一个视频的处理结果。

        payload 格式:
        {
            "video_path": str,
            "insertions": [{start, end, position, folder_label, mode, reason, ...}, ...],
            "materials": [str | None, ...],   # 与 insertions 一一对应（已按 folder_label 抽到的具体素材路径）
            "exec_config": {                   # 本次执行配置（不影响 Tab 5 自身设置，只本次用）
                "extract_mode": ...,
                "mute_material": ...,
                "layout": ...,
                "pip_size": ...,
                "pip_margin": ...,
                "cleanup_mode": ...,
            },
            "auto_run": bool,                  # v1.21：True 表示自动跑这个视频
            "on_finished_callback": callable,  # v1.21：跑完后回调（参数 success: bool）
        }
        """
        try:
            video_path = payload.get("video_path", "")
            insertions = payload.get("insertions", [])
            materials = payload.get("materials", [])
            exec_cfg = payload.get("exec_config", {})
            auto_run = bool(payload.get("auto_run", False))
            callback = payload.get("on_finished_callback")
            ai_output_dir = (payload.get("ai_output_dir") or "").strip()
            output_suffix = (payload.get("output_suffix") or "").strip() or "_混剪"
            # v2.10.14: AI Tab 推过来的输出分辨率（默认 1080P, 跟 Tab 6 同步）
            ai_output_resolution = (payload.get("output_resolution") or "").strip()
            if ai_output_resolution:
                self.resolution.set(ai_output_resolution)
                self._log(f"[AI→Tab5] v2.10.14 同步输出分辨率：{ai_output_resolution}")
            # v1.49: AI Tab 传过来的原始主视频文件夹（用于主视频归档）
            ai_main_video_dir = (payload.get("main_video_dir") or "").strip()

            if not video_path or not insertions:
                self._log("[AI→Tab5] ❌ payload 缺少 video_path 或 insertions")
                if auto_run and callback:
                    try:
                        callback(False)
                    except Exception:
                        pass
                return

            self._log(
                f"[AI→Tab5] 接收：{os.path.basename(video_path)} "
                f"({len(insertions)} 个插入点){' [自动执行]' if auto_run else ''}"
            )

            # 1. v1.21 自动模式：把视频放进临时目录
            # MixScheduler 一次跑一个文件夹里的所有视频，把视频单独放临时目录就能只跑它一个
            self._ai_auto_temp_dir = None
            if auto_run:
                import tempfile, shutil
                tmp_dir = tempfile.mkdtemp(prefix="kele_ai_", dir=tempfile.gettempdir())
                self._ai_auto_temp_dir = tmp_dir
                link_path = os.path.join(tmp_dir, os.path.basename(video_path))
                try:
                    os.symlink(video_path, link_path)
                except Exception:
                    try:
                        shutil.copy(video_path, link_path)
                    except Exception as e:
                        self._log(f"[AI→Tab5] ❌ 临时目录失败：{e}")
                        if callback:
                            try:
                                callback(False)
                            except Exception:
                                pass
                        return
                self.main_video_dir.set(tmp_dir)
                # 流水线模式：清空旧 segments，避免累加
                self.segments = []

                # v1.49: 记录原始主视频文件夹用于归档
                self._ai_archive_dir = ai_main_video_dir if ai_main_video_dir else tmp_dir
                if ai_main_video_dir:
                    self._log(f"[AI→Tab5] v1.49 原始主视频文件夹：{ai_main_video_dir}")
                else:
                    self._log(f"[AI→Tab5] ⚠️ v1.49 AI Tab 未传原始主视频文件夹")

            # 2. 设置主视频路径（保留 main_video_path_var 兼容）
            if hasattr(self, "main_video_path_var"):
                try:
                    self.main_video_path_var.set(video_path)
                except Exception:
                    pass

            # 3. 暂存 exec_config（不覆盖 editing，仅本次使用）
            self._ai_exec_config_override = exec_cfg

            # v1.21：AI 输出目录 + 后缀（避免临时目录被清时丢视频）
            if ai_output_dir:
                try:
                    os.makedirs(ai_output_dir, exist_ok=True)
                except Exception as e:
                    self._log(f"[AI→Tab5] ⚠️ 创建输出目录失败：{e}")
                self.output_dir.set(ai_output_dir)
            if output_suffix:
                self.output_suffix.set(output_suffix)

            # 4. 把每个 insertion 转成 Tab 5 的 segment dict
            added = 0
            for ins, mat in zip(insertions, materials):
                if not mat:
                    continue
                folder = ""
                if mat and os.path.dirname(mat):
                    folder = os.path.dirname(mat)

                start_sec = float(ins.get("start", 0))
                end_sec = float(ins.get("end", 0))

                def _sec_to_mmss(t):
                    if t < 0:
                        t = 0.0
                    m = int(t // 60)
                    s = int(t % 60)
                    cs = int(round((t - int(t)) * 100))
                    if cs >= 100:
                        cs = 99
                    return f"{m:02d}:{s:02d}.{cs:02d}"

                seg = {
                    "start_time": _sec_to_mmss(start_sec),
                    "end_time": _sec_to_mmss(end_sec),
                    "end_unlimited": False,
                    "material_folder": folder,
                    "note": f"[AI] {ins.get('reason', '')} ({ins.get('folder_label', '')})",
                    "extract_mode": ins.get("extract_mode") or exec_cfg.get("extract_mode", "random"),
                    "mute_material": bool(exec_cfg.get("mute_material", True)),
                    "layout": ins.get("position", "replaced"),
                    "pip_size": int(exec_cfg.get("pip_size", 50)),
                    "pip_margin": int(exec_cfg.get("pip_margin", 20)),
                    "cleanup_mode": exec_cfg.get("cleanup_mode", "move"),
                    "cleanup_subdir": "used",
                    "_ai_material_path": mat,
                }
                self.segments.append(seg)
                added += 1

            # 5. 刷新 UI（Tab 5 真正的刷新方法叫 _refresh_segment_list）
            if hasattr(self, "_refresh_segment_list"):
                self._refresh_segment_list()
            elif hasattr(self, "_refresh_segments"):
                self._refresh_segments()
            elif hasattr(self, "_refresh_list"):
                self._refresh_list()

            # 6. 切到 Tab 5 并给用户一个明显的反馈
            try:
                notebook = getattr(self.app, "notebook", None)
                if notebook is not None:
                    tab5_text = "🎬 混剪/插段"
                    for tab_id in notebook.tabs():
                        try:
                            if tab5_text in notebook.tab(tab_id, "text"):
                                notebook.select(tab_id)
                                break
                        except Exception:
                            continue
            except Exception:
                pass

            self._log(f"[AI→Tab5] ✅ 已添加 {added} 个插入点到 Tab 5 队列")
            if not auto_run:
                try:
                    messagebox.showinfo(
                        "AI→Tab 5",
                        f"已接收 {os.path.basename(video_path)} 的 {added} 个插入点\n切到 🎬 混剪/插段 可查看",
                    )
                except Exception:
                    pass

            # 7. v1.21 自动模式：保存 callback + 启动
            if auto_run:
                if callback:
                    self._ai_auto_callback = callback
                self.after(100, self._ai_auto_start_now)

        except Exception as e:
            self._log(f"[AI→Tab5] ❌ 接收失败：{e}")
            import traceback
            self._log(traceback.format_exc())
            cb = payload.get("on_finished_callback") if 'payload' in locals() else None
            if cb:
                try:
                    cb(False)
                except Exception:
                    pass

    def _ai_auto_start_now(self):
        """v1.21：自动模式 - 推完一个后自动点「开始混剪」"""
        try:
            self._log("[AI→Tab5] 🚀 自动启动混剪...")
            # v1.34：保存调度启动前状态，事后判断调度是否真的启动
            self._log(
                f"[AI→Tab5] 状态：segments={len(self.segments)}, "
                f"main_video_dir={self.main_video_dir.get()!r}"
            )
            self._on_start()
            # v1.34：检查 scheduler 是否真的起了（_on_start 可能因 segments 空 / dir 无效早返回）
            if not hasattr(self, "scheduler") or self.scheduler is None or not self.scheduler.is_running():
                self._log("[AI→Tab5] ❌ 调度未启动（_on_start 早返回，无 scheduler 在跑）")
                cb = getattr(self, "_ai_auto_callback", None)
                self._ai_auto_callback = None
                if cb:
                    try:
                        cb({"success": False, "error": "调度未启动（segments/dir 异常）", "stopped": False})
                    except Exception:
                        pass
        except Exception as e:
            self._log(f"[AI→Tab5] ❌ 自动启动失败：{e}")
            cb = getattr(self, "_ai_auto_callback", None)
            self._ai_auto_callback = None
            if cb:
                try:
                    cb({"success": False, "error": str(e), "stopped": False})
                except Exception:
                    pass

    def get_ai_exec_config(self):
        """渲染时调用：优先返回 AI 临时配置，没有则返回 Tab 5 自己的 editing"""
        return getattr(self, "_ai_exec_config_override", None) or getattr(self, "editing", {})


# =================================================================
# main.py 集成入口
# =================================================================
def build_merge_tab(parent, app):
    """main.py 调用：notebook.add(frame, text="🎬 混剪/插段")"""
    frame = MergeTabFrame(parent, app)
    return frame
