"""
可乐口播 v1.5 - 混剪 Tab UI
==========================

完全按 /Applications/可乐混剪.app (laozheng-cutter) 的字段实现：
- 主视频文件夹（批量处理整个目录）
- 输出文件夹
- 插入点列表（10 字段 InsertPoint）
- 输出设置（横竖屏 / 分辨率 / 后缀）
- 全程直嵌卡片（避开 macOS Toplevel bug）

数据模型字段名严格跟 Rust 端 serde 一致：
  MixConfig: mainFolder, mainVideos, insertPoints, outputFolder,
             outputSuffix, outputSettings, mainCleanupMode
  InsertPoint: startTime, endTime, folder, videos, cleanupMode,
               silent, layout, pipScale, orientation, resolution
"""
from __future__ import annotations

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import Optional, Callable

from merge_engine import (
    MixConfig,
    InsertPoint,
    OutputSettings,
    LAYOUT_OPTIONS,
    LAYOUT_LABELS,
    ORIENTATION_OPTIONS,
    ORIENTATION_LABELS,
    RESOLUTION_OPTIONS,
    RESOLUTION_LABELS,
    CLEANUP_OPTIONS,
    CLEANUP_LABELS,
    seconds_to_mmss,
    mmss_to_seconds,
    MixScheduler,
)

CONFIG_FILE = "material_library.json"


# ============================================================
# 主入口
# ============================================================

def build_merge_tab(parent: ttk.Frame, app=None) -> "MergeTabFrame":
    """main.py 调用入口。"""
    return MergeTabFrame(parent, app)


# ============================================================
# 编辑卡片（直嵌在主 Tab 里，避开 macOS Toplevel 白屏 bug）
# ============================================================

class EditorCard(ttk.LabelFrame):
    """
    一个插入点的编辑卡片。

    严格按 InsertPoint 10 字段实现：
      startTime, endTime, folder, videos, cleanupMode,
      silent, layout, pipScale, orientation, resolution
    """

    def __init__(
        self,
        parent,
        on_save: Callable[[InsertPoint], None],
        on_cancel: Callable[[], None],
        initial: Optional[InsertPoint] = None,
        title: str = "新增插入点",
    ):
        super().__init__(parent, text=title, padding=8)
        self.on_save = on_save
        self.on_cancel = on_cancel
        self.ip: InsertPoint = initial or InsertPoint()
        self._build()

    def _build(self):
        # ===== 第 1 行：时间 =====
        time_frame = ttk.Frame(self)
        time_frame.pack(fill="x", pady=4)

        ttk.Label(time_frame, text="开始时间 (分:秒.百分秒):").pack(side="left")
        self.start_m = tk.StringVar(value="00")
        self.start_s = tk.StringVar(value="00")
        self.start_cs = tk.StringVar(value="00")
        for var in (self.start_m, self.start_s, self.start_cs):
            sp = ttk.Spinbox(
                time_frame, from_=0, to=99, width=4,
                textvariable=var, justify="center",
            )
            sp.pack(side="left", padx=2)
            ttk.Label(time_frame, text=":").pack(side="left")
        # 清掉最后一个冒号
        time_frame.winfo_children()[-1].destroy()

        ttk.Label(time_frame, text="  结束时间:").pack(side="left", padx=(20, 0))
        self.end_m = tk.StringVar(value="00")
        self.end_s = tk.StringVar(value="05")
        self.end_cs = tk.StringVar(value="00")
        for var in (self.end_m, self.end_s, self.end_cs):
            sp = ttk.Spinbox(
                time_frame, from_=0, to=99, width=4,
                textvariable=var, justify="center",
            )
            sp.pack(side="left", padx=2)
            ttk.Label(time_frame, text=":").pack(side="left")
        time_frame.winfo_children()[-1].destroy()

        self.unlimited_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            time_frame, text="不限", variable=self.unlimited_var,
        ).pack(side="left", padx=8)

        # 填初值
        sm, ss, sc = seconds_to_mmss(self.ip.startTime)
        self.start_m.set(f"{sm:02d}")
        self.start_s.set(f"{ss:02d}")
        self.start_cs.set(f"{sc:02d}")
        em, es, ec = seconds_to_mmss(self.ip.endTime)
        self.end_m.set(f"{em:02d}")
        self.end_s.set(f"{es:02d}")
        self.end_cs.set(f"{ec:02d}")

        # ===== 第 2 行：素材文件夹 =====
        folder_frame = ttk.Frame(self)
        folder_frame.pack(fill="x", pady=4)

        ttk.Label(folder_frame, text="素材文件夹:").pack(side="left")
        self.folder_var = tk.StringVar(value=self.ip.folder)
        ttk.Entry(folder_frame, textvariable=self.folder_var, width=50).pack(
            side="left", padx=4, fill="x", expand=True,
        )
        ttk.Button(
            folder_frame, text="选择...", command=self._pick_folder,
        ).pack(side="left")
        self.folder_count_lbl = ttk.Label(folder_frame, text="0 个素材", foreground="gray")
        self.folder_count_lbl.pack(side="left", padx=8)
        self.folder_var.trace_add("write", lambda *_: self._refresh_count())

        # ===== 第 3 行：画面布局 + PIP =====
        layout_frame = ttk.Frame(self)
        layout_frame.pack(fill="x", pady=4)

        ttk.Label(layout_frame, text="画面布局:").pack(side="left")
        self.layout_var = tk.StringVar(value=LAYOUT_LABELS.get(self.ip.layout, self.ip.layout))
        layout_cb = ttk.Combobox(
            layout_frame,
            values=[LAYOUT_LABELS[x] for x in LAYOUT_OPTIONS],
            state="readonly",
            width=24,
        )
        layout_cb.set(LAYOUT_LABELS.get(self.ip.layout, self.ip.layout))
        layout_cb.pack(side="left", padx=4)
        layout_cb.bind("<<ComboboxSelected>>", lambda e: self._on_layout_change(layout_cb.get()))

        ttk.Label(layout_frame, text="  PIP 大小:").pack(side="left", padx=(20, 0))
        self.pip_var = tk.DoubleVar(value=self.ip.pipScale)
        self.pip_lbl = ttk.Label(layout_frame, text=f"{int(self.ip.pipScale*100)}%")
        pip_scale = ttk.Scale(
            layout_frame, from_=0.1, to=0.8,
            variable=self.pip_var, orient="horizontal", length=120,
            command=lambda v: self.pip_lbl.configure(text=f"{int(float(v)*100)}%"),
        )
        pip_scale.pack(side="left", padx=4)
        self.pip_lbl.pack(side="left")

        # ===== 第 4 行：插入素材无声 + 抽取方式（隐式，按文件名顺序） =====
        opt_frame = ttk.Frame(self)
        opt_frame.pack(fill="x", pady=4)
        self.silent_var = tk.BooleanVar(value=self.ip.silent)
        ttk.Checkbutton(
            opt_frame, text="🔇 插入素材无声（保留主播原声）",
            variable=self.silent_var,
        ).pack(side="left")

        # ===== 第 5 行：用过的素材处理 =====
        cleanup_frame = ttk.Frame(self)
        cleanup_frame.pack(fill="x", pady=4)
        ttk.Label(cleanup_frame, text="用过的素材:").pack(side="left")
        self.cleanup_var = tk.StringVar(value=CLEANUP_LABELS.get(self.ip.cleanupMode, self.ip.cleanupMode))
        cleanup_cb = ttk.Combobox(
            cleanup_frame,
            values=[CLEANUP_LABELS[x] for x in CLEANUP_OPTIONS],
            state="readonly",
            width=28,
        )
        cleanup_cb.set(CLEANUP_LABELS.get(self.ip.cleanupMode, self.ip.cleanupMode))
        cleanup_cb.pack(side="left", padx=4)
        cleanup_cb.bind("<<ComboboxSelected>>", lambda e: self._on_cleanup_change(cleanup_cb.get()))

        # ===== 第 6 行：横竖屏 + 分辨率 =====
        out_frame = ttk.Frame(self)
        out_frame.pack(fill="x", pady=4)
        ttk.Label(out_frame, text="横竖屏:").pack(side="left")
        self.orient_var = tk.StringVar(value=ORIENTATION_LABELS.get(self.ip.orientation, self.ip.orientation))
        orient_cb = ttk.Combobox(
            out_frame, values=[ORIENTATION_LABELS[x] for x in ORIENTATION_OPTIONS],
            state="readonly", width=14,
        )
        orient_cb.set(ORIENTATION_LABELS.get(self.ip.orientation, self.ip.orientation))
        orient_cb.pack(side="left", padx=4)
        orient_cb.bind("<<ComboboxSelected>>", lambda e: self._on_orient_change(orient_cb.get()))

        ttk.Label(out_frame, text="  分辨率:").pack(side="left", padx=(20, 0))
        self.res_var = tk.StringVar(value=RESOLUTION_LABELS.get(self.ip.resolution, self.ip.resolution))
        res_cb = ttk.Combobox(
            out_frame, values=[RESOLUTION_LABELS[x] for x in RESOLUTION_OPTIONS],
            state="readonly", width=14,
        )
        res_cb.set(RESOLUTION_LABELS.get(self.ip.resolution, self.ip.resolution))
        res_cb.pack(side="left", padx=4)
        res_cb.bind("<<ComboboxSelected>>", lambda e: self._on_res_change(res_cb.get()))

        # ===== 第 7 行：备注 =====
        note_frame = ttk.Frame(self)
        note_frame.pack(fill="x", pady=4)
        ttk.Label(note_frame, text="备注:").pack(side="left")
        self.note_var = tk.StringVar(value=getattr(self.ip, "note", ""))
        ttk.Entry(note_frame, textvariable=self.note_var, width=50).pack(side="left", padx=4, fill="x", expand=True)
        ttk.Label(note_frame, text="（不参与混剪逻辑，仅显示）", foreground="#888").pack(side="left", padx=(4, 0))

        # ===== 底部：取消/保存 =====
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", pady=(8, 0))
        ttk.Button(
            btn_frame, text="取消", command=self.on_cancel, width=10,
        ).pack(side="right", padx=4)
        ttk.Button(
            btn_frame, text="✅ 保存", command=self._save, width=10,
        ).pack(side="right")

        self._refresh_count()

    def _on_layout_change(self, label_text: str):
        """中文 label → 英文 enum 反查，存到 self.ip。"""
        for k, v in LAYOUT_LABELS.items():
            if v == label_text:
                self.ip.layout = k
                break

    def _on_cleanup_change(self, label_text: str):
        for k, v in CLEANUP_LABELS.items():
            if v == label_text:
                self.ip.cleanupMode = k
                break

    def _on_orient_change(self, label_text: str):
        for k, v in ORIENTATION_LABELS.items():
            if v == label_text:
                self.ip.orientation = k
                break

    def _on_res_change(self, label_text: str):
        for k, v in RESOLUTION_LABELS.items():
            if v == label_text:
                self.ip.resolution = k
                break

    def _pick_folder(self):
        d = filedialog.askdirectory(title="选择素材文件夹", initialdir=self.folder_var.get() or None)
        if d:
            self.folder_var.set(d)

    def _refresh_count(self):
        p = self.folder_var.get().strip()
        if p and Path(p).exists():
            exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
            n = sum(
                1 for f in Path(p).iterdir()
                if f.is_file() and f.suffix.lower() in exts
            )
            self.folder_count_lbl.configure(text=f"{n} 个素材", foreground="green" if n > 0 else "red")
        else:
            self.folder_count_lbl.configure(text="0 个素材", foreground="gray")

    def _save(self):
        # 校验
        try:
            sm = int(self.start_m.get() or 0)
            ss = int(self.start_s.get() or 0)
            sc = int(self.start_cs.get() or 0)
            start = mmss_to_seconds(sm, ss, sc)
            if self.unlimited_var.get():
                end = 0.0
            else:
                em = int(self.end_m.get() or 0)
                es = int(self.end_s.get() or 0)
                ec = int(self.end_cs.get() or 0)
                end = mmss_to_seconds(em, es, ec)
        except ValueError:
            messagebox.showerror("时间格式错", "请输入有效数字")
            return

        if not self.unlimited_var.get() and end <= start:
            messagebox.showerror("时间错", "结束时间必须大于开始时间")
            return

        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showerror("缺素材", "请选择素材文件夹")
            return
        if not Path(folder).exists():
            messagebox.showerror("路径不存在", f"素材文件夹不存在：\n{folder}")
            return

        # 写入
        self.ip.startTime = start
        self.ip.endTime = end
        self.ip.folder = folder
        self.ip.cleanupMode = self.cleanup_var.get()
        self.ip.silent = self.silent_var.get()
        self.ip.layout = self.layout_var.get()
        self.ip.pipScale = self.pip_var.get()
        self.ip.orientation = self.orient_var.get()
        self.ip.resolution = self.res_var.get()
        self.ip.note = self.note_var.get().strip()
        self.on_save(self.ip)


# ============================================================
# 插入点列表卡片（一行一张，显示 10 字段全貌）
# ============================================================

class SegmentCard(ttk.Frame):
    """
    单个插入点的卡片视图。

    布局：
      ┌────────────────────────────────────────────────────────────┐
      │ # 1   00:02.00  →  00:05.00    [✏ 编辑] [❌ 删除] [↑] [↓]  │
      │ 📁 素材: /Users/zxz/.../口播素材/1                          │
      │ 🎬 布局: 替换主材画面（黑边填）   🔇 无声   📦 用过: 移到 used/  │
      │ 📝 备注: 大理 / 云南                                         │
      └────────────────────────────────────────────────────────────┘
    """

    # 选中 / 未选中配色
    COLORS = {
        "bg_normal": "#FAFAFA",
        "bg_selected": "#FFE9A8",  # 浅黄色高亮
        "fg_label": "#666",
        "fg_value": "#222",
        "fg_time": "#0066CC",  # 时段用蓝色突出
        "fg_idx": "#999",
    }

    def __init__(
        self,
        parent,
        idx: int,
        ip: InsertPoint,
        on_edit,
        on_delete,
        on_move_up,
        on_move_down,
    ):
        super().__init__(parent, relief="ridge", borderwidth=1, padding=8)
        self.idx = idx
        self.ip = ip
        self._selected = False
        self._on_edit = on_edit
        self._on_delete = on_delete
        self._on_move_up = on_move_up
        self._on_move_down = on_move_down

        self._build()

        # 双击编辑
        self.bind("<Double-Button-1>", lambda e: on_edit(idx))
        for child in self.winfo_children():
            child.bind("<Double-Button-1>", lambda e: on_edit(idx))

        # 单击选中
        self.bind("<Button-1>", self._on_click_select)
        for child in self.winfo_children():
            child.bind("<Button-1>", self._on_click_select)

    def _build(self):
        # ===== 第 1 行：#编号 + 时段 + 操作按钮 =====
        row1 = ttk.Frame(self)
        row1.pack(fill="x")

        idx_label = tk.Label(
            row1, text=f"#{self.idx + 1}",
            font=("Helvetica", 13, "bold"),
            fg=self.COLORS["fg_idx"],
        )
        idx_label.pack(side="left")

        # 时段（大字 + 蓝色突出）
        m1, s1, c1 = seconds_to_mmss(self.ip.startTime)
        if self.ip.endTime > 0:
            m2, s2, c2 = seconds_to_mmss(self.ip.endTime)
            time_text = f"{m1:02d}:{s1:02d}.{c1:02d}  →  {m2:02d}:{s2:02d}.{c2:02d}"
        else:
            time_text = f"{m1:02d}:{s1:02d}.{c1:02d}  →  不限"
        time_label = tk.Label(
            row1, text=f"  ⏱  {time_text}",
            font=("Menlo", 14, "bold"),
            fg=self.COLORS["fg_time"],
        )
        time_label.pack(side="left", padx=(8, 0))

        # 右侧操作按钮
        ttk.Button(row1, text="✏ 编辑", width=6, command=lambda: self._on_edit(self.idx)).pack(side="right")
        ttk.Button(row1, text="❌ 删除", width=6, command=lambda: self._on_delete(self.idx)).pack(side="right", padx=2)
        ttk.Button(row1, text="↓", width=3, command=lambda: self._on_move_down(self.idx)).pack(side="right", padx=2)
        ttk.Button(row1, text="↑", width=3, command=lambda: self._on_move_up(self.idx)).pack(side="right", padx=2)

        # ===== 第 2 行：素材文件夹 + 横竖屏 + 分辨率 =====
        row2 = ttk.Frame(self)
        row2.pack(fill="x", pady=(6, 0))
        folder_text = self.ip.folder if self.ip.folder else "（未选）"
        if len(folder_text) > 70:
            folder_text = "..." + folder_text[-67:]
        tk.Label(row2, text=f"📁 {folder_text}", fg=self.COLORS["fg_value"]).pack(side="left")

        orient_text = ORIENTATION_LABELS.get(self.ip.orientation, self.ip.orientation)
        res_text = RESOLUTION_LABELS.get(self.ip.resolution, self.ip.resolution)
        tk.Label(
            row2, text=f"    📐 {orient_text} · {res_text}",
            fg=self.COLORS["fg_label"],
        ).pack(side="right")

        # ===== 第 3 行：布局 + 无声 + PIP + 用过处理 =====
        row3 = ttk.Frame(self)
        row3.pack(fill="x", pady=(4, 0))
        layout_text = LAYOUT_LABELS.get(self.ip.layout, self.ip.layout)
        silent_text = "🔇 无声（保留主播原声）" if self.ip.silent else "🔊 有声（混素材原声）"
        # PIP 只有 PIP 系列布局才有意义
        is_pip = self.ip.layout.startswith("pip_")
        pip_text = f"· PIP {int(self.ip.pipScale * 100)}%" if is_pip else ""
        cleanup_text = CLEANUP_LABELS.get(self.ip.cleanupMode, self.ip.cleanupMode)

        tk.Label(row3, text=f"🎬 {layout_text} {pip_text}", fg=self.COLORS["fg_value"]).pack(side="left")
        tk.Label(row3, text=f"    {silent_text}", fg=self.COLORS["fg_label"]).pack(side="left", padx=(8, 0))
        tk.Label(row3, text=f"    📦 {cleanup_text}", fg=self.COLORS["fg_label"]).pack(side="left", padx=(8, 0))

        # ===== 第 4 行：备注 =====
        if self.ip.note:
            row4 = ttk.Frame(self)
            row4.pack(fill="x", pady=(4, 0))
            note_text = self.ip.note if len(self.ip.note) <= 60 else "..." + self.ip.note[-57:]
            tk.Label(row4, text=f"📝 {note_text}", fg="#888", wraplength=600, justify="left").pack(side="left")

    def _on_click_select(self, event=None):
        # 取消所有兄弟的选中，选中自己
        parent = self.master
        if parent is None:
            return
        for w in parent.winfo_children():
            if isinstance(w, SegmentCard) and w is not self:
                w.deselect()
        self.select()
        return "break"

    def is_selected(self) -> bool:
        return self._selected

    def select(self):
        self._selected = True
        self.configure(style="SelectedCard.TFrame")
        # 给所有子 label 改背景色
        self._set_bg(self.COLORS["bg_selected"])

    def deselect(self):
        self._selected = False
        self.configure(style="TFrame")
        self._set_bg(self.COLORS["bg_normal"])

    def _set_bg(self, color: str):
        try:
            self.configure(bg=color)
        except Exception:
            pass
        for w in self.winfo_children():
            try:
                w.configure(bg=color)
            except Exception:
                pass
            for c in w.winfo_children():
                try:
                    c.configure(bg=color)
                except Exception:
                    pass


# ============================================================
# 主 Tab 控件
# ============================================================

class MergeTabFrame:
    """混剪 Tab 主控件（5 个区：主视频 / 输出 / 插入点列表 / 输出设置 / 操作+日志）。"""

    def __init__(self, parent: ttk.Frame, app=None):
        self.parent = parent
        self.app = app
        self.config = self._load_config()
        self.scheduler: Optional[MixScheduler] = None
        self._segment_cards = []  # 当前显示的所有插入点卡片
        self._build()
        self._refresh_list()

    # ---------- UI 构建 ----------

    def _build(self):
        # === 外层：可滚动容器（解决 macOS 窗口矮时按钮/日志被裁的问题） ===
        outer = ttk.Frame(self.parent)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0, bd=0)
        canvas.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)

        # 内容 frame
        body = ttk.Frame(canvas)
        body_win = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_config(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        body.bind("<Configure>", _on_body_config)

        def _on_canvas_config(event):
            canvas.itemconfigure(body_win, width=event.width)
        canvas.bind("<Configure>", _on_canvas_config)

        # 鼠标滚轮（macOS 用 <MouseWheel>，delta 正负）
        def _on_mousewheel(event):
            delta = -1 if event.delta > 0 else 1
            if abs(event.delta) >= 120:
                delta = -2 if event.delta > 0 else 2
            canvas.yview_scroll(delta, "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # 之后所有 pack 都打到 body 上（不是 self.parent）
        content = body

        # === 区 1：主视频文件夹 ===
        sec1 = ttk.LabelFrame(content, text="⏺️ 主视频文件夹（批量处理此目录下所有视频）", padding=8)
        sec1.pack(fill="x", pady=4)
        self.main_folder_var = tk.StringVar(value=self.config.mainFolder)
        ttk.Entry(sec1, textvariable=self.main_folder_var, width=70).pack(side="left", fill="x", expand=True)
        ttk.Button(sec1, text="选择...", command=self._pick_main_folder).pack(side="left", padx=4)

        # === 区 2：输出文件夹 ===
        sec2 = ttk.LabelFrame(content, text="📤 输出文件夹", padding=8)
        sec2.pack(fill="x", pady=4)
        self.output_folder_var = tk.StringVar(value=self.config.outputFolder)
        ttk.Entry(sec2, textvariable=self.output_folder_var, width=70).pack(side="left", fill="x", expand=True)
        ttk.Button(sec2, text="选择...", command=self._pick_output_folder).pack(side="left", padx=4)
        ttk.Button(sec2, text="=主视频文件夹", command=self._copy_main_to_output).pack(side="left", padx=4)

        # === 区 3：插入点列表（卡片列表，每张卡片显示一个插入点的全 10 字段）===
        sec3 = ttk.LabelFrame(content, text="📋 插入点列表", padding=8)
        sec3.pack(fill="both", expand=True, pady=4)

        # 工具栏（添加 + 保存/加载）
        toolbar = ttk.Frame(sec3)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="➕ 添加插入点", command=self._on_add).pack(side="left")
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="💾 保存配置", command=self._save_config_to_disk).pack(side="left")
        ttk.Button(toolbar, text="📂 加载配置", command=self._load_config_dialog).pack(side="left", padx=4)
        ttk.Label(toolbar, text="    💡 双击卡片可编辑", foreground="#888").pack(side="left")

        # 编辑卡片容器（默认隐藏）
        self.editor_container = ttk.Frame(sec3)
        self.editor_container.pack(fill="x", pady=4)

        # 卡片列表容器（带滚动）
        list_wrap = ttk.Frame(sec3)
        list_wrap.pack(fill="both", expand=True, pady=4)

        self.list_canvas = tk.Canvas(list_wrap, highlightthickness=0, height=220)
        list_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=list_scroll.set)
        list_scroll.pack(side="right", fill="y")
        self.list_canvas.pack(side="left", fill="both", expand=True)

        self.list_inner = ttk.Frame(self.list_canvas)
        self.list_canvas.create_window((0, 0), window=self.list_inner, anchor="nw")
        self.list_inner.bind(
            "<Configure>",
            lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")),
        )

        # 让鼠标滚轮在 list_canvas 区域能滚动
        def _on_wheel(event):
            self.list_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.list_canvas.bind("<Enter>", lambda e: self.list_canvas.bind_all("<MouseWheel>", _on_wheel))
        self.list_canvas.bind("<Leave>", lambda e: self.list_canvas.unbind_all("<MouseWheel>"))

        # === 区 4：输出设置 ===
        sec4 = ttk.LabelFrame(content, text="🎚️ 输出设置", padding=8)
        sec4.pack(fill="x", pady=4)
        ttk.Label(sec4, text="文件名后缀:").pack(side="left")
        self.suffix_var = tk.StringVar(value=self.config.outputSuffix)
        ttk.Entry(sec4, textvariable=self.suffix_var, width=10).pack(side="left", padx=4)
        ttk.Label(sec4, text="  横竖屏:").pack(side="left", padx=(20, 0))
        self.orient_var = tk.StringVar(value=ORIENTATION_LABELS.get(self.config.outputSettings.orientation, self.config.outputSettings.orientation))
        self.orient_combo = ttk.Combobox(
            sec4,
            values=[ORIENTATION_LABELS[x] for x in ORIENTATION_OPTIONS],
            state="readonly", width=14,
        )
        self.orient_combo.set(ORIENTATION_LABELS.get(self.config.outputSettings.orientation, self.config.outputSettings.orientation))
        self.orient_combo.pack(side="left", padx=4)
        ttk.Label(sec4, text="  分辨率:").pack(side="left")
        self.res_var = tk.StringVar(value=RESOLUTION_LABELS.get(self.config.outputSettings.resolution, self.config.outputSettings.resolution))
        self.res_combo = ttk.Combobox(
            sec4,
            values=[RESOLUTION_LABELS[x] for x in RESOLUTION_OPTIONS],
            state="readonly", width=14,
        )
        self.res_combo.set(RESOLUTION_LABELS.get(self.config.outputSettings.resolution, self.config.outputSettings.resolution))
        self.res_combo.pack(side="left", padx=4)
        ttk.Label(sec4, text="  主视频清理:").pack(side="left", padx=(20, 0))
        self.main_cleanup_var = tk.StringVar(value=CLEANUP_LABELS.get(self.config.mainCleanupMode, self.config.mainCleanupMode))
        self.main_cleanup_combo = ttk.Combobox(
            sec4,
            values=[CLEANUP_LABELS[x] for x in CLEANUP_OPTIONS],
            state="readonly", width=28,
        )
        self.main_cleanup_combo.set(CLEANUP_LABELS.get(self.config.mainCleanupMode, self.config.mainCleanupMode))
        self.main_cleanup_combo.pack(side="left", padx=4)

        # === 区 5：开始混剪（显眼位置：单独一行，红底） ===
        op_frame = ttk.Frame(content)
        op_frame.pack(fill="x", pady=8)
        self.start_btn = tk.Button(
            op_frame, text="🚀  开 始 混 剪",
            command=self._on_start,
            bg="#2d7d46", fg="white",
            font=("Helvetica", 14, "bold"),
            relief="raised", bd=2, height=2,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=4)
        self.stop_btn = tk.Button(
            op_frame, text="⏹ 停止",
            command=self._on_stop,
            state="disabled",
            bg="#a04040", fg="white",
            font=("Helvetica", 12),
        )
        self.stop_btn.pack(side="left", padx=4)
        self.progress_lbl = ttk.Label(op_frame, text="", font=("Helvetica", 11))
        self.progress_lbl.pack(side="left", padx=10)

        # === 区 6：日志（永远在底部，可扩展：随窗口拉大而增高） ===
        log_frame = ttk.LabelFrame(content, text="📋 运行日志（混剪过程中看这里）", padding=4)
        log_frame.pack(fill="both", expand=True, pady=4)  # ← 改 fill="both" + expand=True，日志框随窗口拉大
        self.log_text = tk.Text(
            log_frame, height=8, wrap="word",  # ← height 从 12 降到 8（最小值），expand 拉大
            font=("Menlo", 11), bg="#1e1e1e", fg="#e0e0e0",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        # 滚动条
        sb = ttk.Scrollbar(self.log_text, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

    # ---------- 文件夹选择 ----------

    def _pick_main_folder(self):
        d = filedialog.askdirectory(title="选择主视频文件夹", initialdir=self.main_folder_var.get() or None)
        if d:
            self.main_folder_var.set(d)

    def _pick_output_folder(self):
        d = filedialog.askdirectory(title="选择输出文件夹", initialdir=self.output_folder_var.get() or None)
        if d:
            self.output_folder_var.set(d)

    def _copy_main_to_output(self):
        if self.main_folder_var.get():
            self.output_folder_var.set(self.main_folder_var.get())

    # ---------- 插入点编辑 ----------

    def _hide_editor(self):
        for w in self.editor_container.winfo_children():
            w.destroy()
        self._editor_card = None

    def _show_editor(self, ip: Optional[InsertPoint] = None, edit_idx: Optional[int] = None):
        self._hide_editor()
        title = f"插入点 {edit_idx + 1} - 编辑" if edit_idx is not None else "插入点 - 新增"
        card = EditorCard(
            self.editor_container,
            on_save=lambda new_ip: self._on_save_card(new_ip, edit_idx),
            on_cancel=self._hide_editor,
            initial=ip,
            title=title,
        )
        card.pack(fill="x")
        self._editor_card = card

    def _on_add(self):
        self._show_editor(ip=None, edit_idx=None)

    def _on_edit(self):
        idx = self._get_selected_idx()
        if idx is None:
            messagebox.showinfo("提示", "请先选中一张卡片")
            return
        if 0 <= idx < len(self.config.insertPoints):
            self._show_editor(ip=self.config.insertPoints[idx], edit_idx=idx)

    def _on_save_card(self, ip: InsertPoint, edit_idx: Optional[int]):
        if edit_idx is None:
            self.config.insertPoints.append(ip)
        else:
            self.config.insertPoints[edit_idx] = ip
        self._hide_editor()
        self._refresh_list()
        self._save_config_to_disk(silent=True)

    def _on_delete(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(self.tree.item(sel[0], "tags")[0])
        if messagebox.askyesno("确认", f"删除插入点 {idx + 1}？"):
            del self.config.insertPoints[idx]
            self._refresh_list()
            self._save_config_to_disk(silent=True)

    def _move(self, delta: int):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(self.tree.item(sel[0], "tags")[0])
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.config.insertPoints)):
            return
        self.config.insertPoints[idx], self.config.insertPoints[new_idx] = (
            self.config.insertPoints[new_idx], self.config.insertPoints[idx]
        )
        self._refresh_list()
        new_iid = self.tree.get_children()[new_idx]
        self.tree.selection_set(new_iid)

    def _on_delete(self):
        idx = self._get_selected_idx()
        if idx is None:
            return
        if messagebox.askyesno("确认", f"删除插入点 {idx + 1}？"):
            del self.config.insertPoints[idx]
            self._refresh_list()
            self._save_config_to_disk(silent=True)

    def _move(self, delta: int):
        idx = self._get_selected_idx()
        if idx is None:
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.config.insertPoints)):
            return
        self.config.insertPoints[idx], self.config.insertPoints[new_idx] = (
            self.config.insertPoints[new_idx], self.config.insertPoints[idx]
        )
        self._refresh_list()
        # 重新选中
        if 0 <= new_idx < len(self._segment_cards):
            self._segment_cards[new_idx].select()

    def _get_selected_idx(self) -> Optional[int]:
        """返回当前选中的插入点 idx，没有选中返回 None。"""
        for i, card in enumerate(self._segment_cards):
            if card.is_selected():
                return i
        return None

    def _refresh_list(self):
        """重建卡片列表。"""
        # 清空
        for w in self.list_inner.winfo_children():
            w.destroy()
        self._segment_cards = []

        if not self.config.insertPoints:
            empty = ttk.Label(
                self.list_inner,
                text="（暂无插入点，点上方 ➕ 添加插入点）",
                foreground="#888",
                padding=20,
            )
            empty.pack(fill="x")
            return

        for i, ip in enumerate(self.config.insertPoints):
            card = SegmentCard(
                self.list_inner,
                idx=i,
                ip=ip,
                on_edit=self._on_edit_idx,
                on_delete=self._on_delete_idx,
                on_move_up=lambda idx=i: self._move_idx(idx, -1),
                on_move_down=lambda idx=i: self._move_idx(idx, 1),
            )
            card.pack(fill="x", pady=3)
            self._segment_cards.append(card)

    def _on_edit_idx(self, idx: int):
        if 0 <= idx < len(self.config.insertPoints):
            self._show_editor(ip=self.config.insertPoints[idx], edit_idx=idx)

    def _on_delete_idx(self, idx: int):
        if messagebox.askyesno("确认", f"删除插入点 {idx + 1}？"):
            del self.config.insertPoints[idx]
            self._refresh_list()
            self._save_config_to_disk(silent=True)

    def _move_idx(self, idx: int, delta: int):
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.config.insertPoints)):
            return
        self.config.insertPoints[idx], self.config.insertPoints[new_idx] = (
            self.config.insertPoints[new_idx], self.config.insertPoints[idx]
        )
        self._refresh_list()
        if 0 <= new_idx < len(self._segment_cards):
            self._segment_cards[new_idx].select()

    def _refresh_list_legacy_removed(self):
        """(老 Treeview 实现，保留以防 _refresh_list() 别处被旧代码调用)"""
        pass

    # ---------- 配置持久化 ----------

    def _config_path(self) -> Path:
        return Path(__file__).parent / CONFIG_FILE

    def _save_config_to_disk(self, silent: bool = False):
        # 先把界面上的值同步到 config
        self.config.mainFolder = self.main_folder_var.get().strip()
        self.config.outputFolder = self.output_folder_var.get().strip()
        self.config.outputSuffix = self.suffix_var.get().strip() or "_混剪"
        self.config.outputSettings.orientation = self.orient_var.get()
        self.config.outputSettings.resolution = self.res_var.get()
        self.config.mainCleanupMode = self.main_cleanup_var.get()

        try:
            with open(self._config_path(), "w", encoding="utf-8") as f:
                json.dump(self.config.to_dict(), f, ensure_ascii=False, indent=2)
            if not silent:
                self._log(f"💾 配置已保存 → {self._config_path().name}")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _load_config(self) -> MixConfig:
        p = self._config_path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return MixConfig.from_dict(json.load(f))
            except Exception as e:
                print(f"加载配置失败: {e}")
        return MixConfig()

    def _load_config_dialog(self):
        f = filedialog.askopenfilename(
            title="加载配置",
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
            initialdir=str(self._config_path().parent),
        )
        if f:
            try:
                with open(f, "r", encoding="utf-8") as f_obj:
                    self.config = MixConfig.from_dict(json.load(f_obj))
                # 同步界面
                self.main_folder_var.set(self.config.mainFolder)
                self.output_folder_var.set(self.config.outputFolder)
                self.suffix_var.set(self.config.outputSuffix)
                self.orient_combo.set(ORIENTATION_LABELS.get(self.config.outputSettings.orientation, self.config.outputSettings.orientation))
                self.res_combo.set(RESOLUTION_LABELS.get(self.config.outputSettings.resolution, self.config.outputSettings.resolution))
                self.main_cleanup_combo.set(CLEANUP_LABELS.get(self.config.mainCleanupMode, self.config.mainCleanupMode))
                self._refresh_list()
                self._log(f"✅ 已加载配置: {Path(f).name}")
            except Exception as e:
                messagebox.showerror("加载失败", str(e))

    # ---------- 运行 ----------

    def _on_start(self):
        # 同步 config（中英文反查）
        self.config.mainFolder = self.main_folder_var.get().strip()
        self.config.outputFolder = self.output_folder_var.get().strip()
        self.config.outputSuffix = self.suffix_var.get().strip() or "_混剪"
        # 横竖屏（中文 label → enum）
        for k, v in ORIENTATION_LABELS.items():
            if v == self.orient_combo.get():
                self.config.outputSettings.orientation = k
                break
        for k, v in RESOLUTION_LABELS.items():
            if v == self.res_combo.get():
                self.config.outputSettings.resolution = k
                break
        for k, v in CLEANUP_LABELS.items():
            if v == self.main_cleanup_combo.get():
                self.config.mainCleanupMode = k
                break

        # 校验
        if not self.config.mainFolder:
            messagebox.showerror("缺主视频", "请选择主视频文件夹")
            return
        if not Path(self.config.mainFolder).exists():
            messagebox.showerror("路径不存在", f"主视频文件夹不存在：\n{self.config.mainFolder}")
            return
        if not self.config.insertPoints:
            if not messagebox.askyesno("无插入点", "没有任何插入点，将只复制主视频（不改任何内容），继续？"):
                return

        # 保存配置
        self._save_config_to_disk(silent=True)

        # 启动调度器
        self._clear_log()
        self.scheduler = MixScheduler(
            self.config,
            log_callback=self._log,
            progress_callback=lambda cur, total, name: self.progress_lbl.configure(
                text=f"[{cur}/{total}] {name}",
            ),
            on_finished=self._on_scheduler_finished,
        )
        self.start_btn.configure(state="disabled", bg="#888888")
        self.stop_btn.configure(state="normal", bg="#a04040")
        self.scheduler.start()

    def _on_scheduler_finished(self, success: bool):
        """scheduler 跑完（成功/失败/异常）自动调，从子线程调，需切回主线程。"""
        def _reset_ui():
            self.start_btn.configure(state="normal", bg="#2d7d46")
            self.stop_btn.configure(state="disabled", bg="#888888")
            if success:
                self.progress_lbl.configure(text="✅ 完成")
            else:
                if self.scheduler and self.scheduler.was_stopped():
                    self.progress_lbl.configure(text="⏹ 已停止")
                else:
                    self.progress_lbl.configure(text="❌ 失败（看日志）")
        try:
            self.parent.after(0, _reset_ui)
        except Exception:
            _reset_ui()

    def _on_stop(self):
        if self.scheduler:
            self.scheduler.stop()

    # ---------- 日志 ----------

    def _log(self, msg: str):
        def append():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        try:
            self.parent.after(0, append)
        except Exception:
            append()

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")