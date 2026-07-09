"""
可乐口播 v1.5 - 混剪 Tab UI（v2 重写）
====================================

布局：左 40% 插入点列表 + 右 60% 详情/编辑面板
特点：
- 顶部固定：主视频文件夹 + 输出文件夹
- 中间可拖动分栏：左列表 / 右编辑
- 底部固定：输出设置 + 开始按钮 + 日志区
- 编辑表单直嵌右 60%（不弹窗，绕开 macOS Toplevel 白屏 bug）

数据模型：跟 merge_engine.py 的 MixConfig / InsertPoint 完全一致
"""
from __future__ import annotations

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import Optional

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
# 主入口（main.py 调用）
# ============================================================

def build_merge_tab(parent: ttk.Frame, app=None) -> "MergeTabFrame":
    return MergeTabFrame(parent, app)


# ============================================================
# 插入点卡片（左侧列表项）
# ============================================================

class SegmentCard(tk.Frame):
    """左侧列表里的一行（一个插入点）。"""

    def __init__(self, parent, idx: int, ip: InsertPoint, on_click, on_double_click):
        super().__init__(parent, relief="ridge", bd=1, bg="white", padx=6, pady=4)
        self.idx = idx
        self.ip = ip
        self._on_click = on_click
        self._on_double_click = on_double_click
        self._selected = False

        # 顶行：编号 + 时段
        top = tk.Frame(self, bg="white")
        top.pack(fill="x")
        tk.Label(top, text=f"#{idx+1}", font=("Helvetica", 11, "bold"),
                 bg="white", fg="#444").pack(side="left")
        tk.Label(top, text="  ⏱ ", font=("Helvetica", 11), bg="white").pack(side="left")
        time_str = self._format_time()
        tk.Label(top, text=time_str, font=("Menlo", 12, "bold"),
                 bg="white", fg="#1a73e8").pack(side="left")

        # 第二行：素材文件夹（截断显示）
        folder_name = Path(ip.folder).name if ip.folder else ""
        if not folder_name:
            folder_name = "（未选素材）"
        tk.Label(self, text=f"📁 {folder_name[:30]}", font=("Helvetica", 10),
                 bg="white", fg="#666", anchor="w").pack(fill="x")

        # 第三行：画面布局 + 横竖屏
        layout_zh = LAYOUT_LABELS.get(ip.layout, ip.layout)
        orient_zh = ORIENTATION_LABELS.get(ip.orientation, ip.orientation)
        tk.Label(self, text=f"🎬 {layout_zh}  ·  📐 {orient_zh}",
                 font=("Helvetica", 9), bg="white", fg="#888",
                 anchor="w").pack(fill="x")

        # 绑定点击
        for w in (self, top, *top.winfo_children(), *self.winfo_children()[1:]):
            try:
                w.bind("<Button-1>", self._handle_click)
                w.bind("<Double-Button-1>", self._handle_double)
            except Exception:
                pass

    def _format_time(self) -> str:
        s = self.ip.startTime
        e = self.ip.endTime if self.ip.endTime is not None else None
        s_str = seconds_to_mmss(s)
        e_str = "不限" if e is None else seconds_to_mmss(e)
        return f"{s_str} → {e_str}"

    def _handle_click(self, _e):
        self._on_click(self.idx)

    def _handle_double(self, _e):
        self._on_double_click(self.idx)

    def set_selected(self, selected: bool):
        self._selected = selected
        bg = "#e3f2fd" if selected else "white"
        self.configure(bg=bg)
        for child in self.winfo_children():
            try:
                child.configure(bg=bg)
                for sub in child.winfo_children():
                    try:
                        sub.configure(bg=bg)
                    except Exception:
                        pass
            except Exception:
                pass


# ============================================================
# 编辑表单（右侧面板）
# ============================================================

class EditorPanel(ttk.LabelFrame):
    """右 60% 的编辑表单，直嵌主 Tab（不弹窗）。"""

    def __init__(self, parent, on_save, on_delete, on_clear):
        super().__init__(parent, text="📝 详情/编辑面板", padding=8)
        self._on_save = on_save
        self._on_delete = on_delete
        self._on_clear = on_clear
        self._current_idx: Optional[int] = None
        self._build()

    def _build(self):
        # 空状态提示
        self._empty_label = ttk.Label(
            self,
            text="👈 请在左侧选中一个插入点\n或点击下方 ➕ 添加插入点",
            foreground="#888",
            font=("Helvetica", 11),
            justify="center",
        )

        # 编辑字段容器
        self._form = ttk.Frame(self)
        self._build_form(self._form)
        self.show_empty()

    def _build_form(self, parent):
        # === 时段 ===
        time_box = ttk.LabelFrame(parent, text="⏱ 时段", padding=6)
        time_box.pack(fill="x", pady=4)
        ttk.Label(time_box, text="开始 (分:秒.百分秒):").grid(row=0, column=0, sticky="w", padx=2)
        self.start_m = tk.StringVar(value="00")
        self.start_s = tk.StringVar(value="00")
        self.start_cs = tk.StringVar(value="00")
        for col, var in enumerate((self.start_m, self.start_s, self.start_cs), 1):
            ttk.Spinbox(time_box, from_=0, to=99, width=4,
                        textvariable=var, justify="center").grid(row=0, column=col, padx=2)
        ttk.Label(time_box, text="   结束:").grid(row=0, column=4, padx=(20, 2))
        self.end_m = tk.StringVar(value="00")
        self.end_s = tk.StringVar(value="00")
        self.end_cs = tk.StringVar(value="00")
        for col, var in enumerate((self.end_m, self.end_s, self.end_cs), 5):
            ttk.Spinbox(time_box, from_=0, to=99, width=4,
                        textvariable=var, justify="center").grid(row=0, column=col, padx=2)
        self.end_unlimited_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(time_box, text="留空不限", variable=self.end_unlimited_var
                        ).grid(row=0, column=8, padx=(20, 0))

        # === 素材文件夹 ===
        folder_box = ttk.LabelFrame(parent, text="📁 素材文件夹", padding=6)
        folder_box.pack(fill="x", pady=4)
        self.folder_var = tk.StringVar()
        ttk.Entry(folder_box, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(folder_box, text="📂 选择", command=self._pick_folder).pack(side="left")
        self.folder_count_lbl = ttk.Label(folder_box, text="（未选）", foreground="#888")
        self.folder_count_lbl.pack(side="left", padx=8)
        self.folder_var.trace_add("write", lambda *_: self._refresh_folder_count())

        # === 抽取方式 + 无声 ===
        opt_box = ttk.LabelFrame(parent, text="⚙️ 抽取选项", padding=6)
        opt_box.pack(fill="x", pady=4)
        ttk.Label(opt_box, text="抽取方式:").pack(side="left")
        self.pick_mode_var = tk.StringVar(value="顺序")
        ttk.Combobox(opt_box, textvariable=self.pick_mode_var,
                     values=["顺序", "随机"], state="readonly", width=8).pack(side="left", padx=4)
        self.silent_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_box, text="🔇 插入素材无声（保留主材原声）",
                        variable=self.silent_var).pack(side="left", padx=20)

        # === 画面布局 + PIP 大小 ===
        layout_box = ttk.LabelFrame(parent, text="🎬 画面布局", padding=6)
        layout_box.pack(fill="x", pady=4)
        ttk.Label(layout_box, text="布局:").grid(row=0, column=0, sticky="w")
        self.layout_var = tk.StringVar()
        self.layout_combo = ttk.Combobox(
            layout_box,
            values=[LAYOUT_LABELS[x] for x in LAYOUT_OPTIONS],
            state="readonly", width=24,
        )
        self.layout_combo.grid(row=0, column=1, padx=4, sticky="w")
        self.layout_combo.bind("<<ComboboxSelected>>", lambda _: self._refresh_pip_visibility())

        ttk.Label(layout_box, text="PIP 大小:").grid(row=0, column=2, padx=(20, 4), sticky="e")
        self.pip_var = tk.DoubleVar(value=0.5)
        self.pip_scale = ttk.Scale(layout_box, from_=0.1, to=0.8,
                                   variable=self.pip_var, orient="horizontal", length=120)
        self.pip_scale.grid(row=0, column=3, sticky="w")
        self.pip_lbl = ttk.Label(layout_box, text="50%")
        self.pip_lbl.grid(row=0, column=4, padx=4)
        self.pip_var.trace_add("write", lambda *_: self.pip_lbl.configure(
            text=f"{int(self.pip_var.get()*100)}%"))

        # === 横竖屏 + 分辨率 ===
        out_box = ttk.LabelFrame(parent, text="📐 横竖屏 + 分辨率", padding=6)
        out_box.pack(fill="x", pady=4)
        ttk.Label(out_box, text="横竖屏:").pack(side="left")
        self.orient_var = tk.StringVar(value=ORIENTATION_LABELS["landscape"])
        ttk.Combobox(out_box, textvariable=self.orient_var,
                     values=[ORIENTATION_LABELS[x] for x in ORIENTATION_OPTIONS],
                     state="readonly", width=12).pack(side="left", padx=4)
        ttk.Label(out_box, text="  分辨率:").pack(side="left")
        self.res_var = tk.StringVar(value=RESOLUTION_LABELS["720p"])
        ttk.Combobox(out_box, textvariable=self.res_var,
                     values=[RESOLUTION_LABELS[x] for x in RESOLUTION_OPTIONS],
                     state="readonly", width=14).pack(side="left", padx=4)

        # === 用过处理 + 备注 ===
        misc_box = ttk.LabelFrame(parent, text="📦 用过处理 + 备注", padding=6)
        misc_box.pack(fill="x", pady=4)
        ttk.Label(misc_box, text="用过处理:").grid(row=0, column=0, sticky="w")
        self.cleanup_var = tk.StringVar(value=CLEANUP_LABELS["archive_to_subfolder"])
        self.cleanup_combo = ttk.Combobox(
            misc_box,
            values=[CLEANUP_LABELS[x] for x in CLEANUP_OPTIONS],
            state="readonly", width=24,
        )
        self.cleanup_combo.grid(row=0, column=1, padx=4, sticky="w")
        ttk.Label(misc_box, text="子目录:").grid(row=0, column=2, padx=(20, 4), sticky="e")
        self.subdir_var = tk.StringVar(value="used")
        ttk.Entry(misc_box, textvariable=self.subdir_var, width=10).grid(row=0, column=3, sticky="w")

        ttk.Label(misc_box, text="备注:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.note_var = tk.StringVar()
        ttk.Entry(misc_box, textvariable=self.note_var).grid(
            row=1, column=1, columnspan=3, sticky="we", pady=(8, 0))

        # === 按钮区 ===
        btn_box = ttk.Frame(parent)
        btn_box.pack(fill="x", pady=8)
        self.save_btn = ttk.Button(btn_box, text="💾 保存", command=self._save)
        self.save_btn.pack(side="left", padx=4)
        self.delete_btn = ttk.Button(btn_box, text="🗑 删除", command=self._delete)
        self.delete_btn.pack(side="left", padx=4)
        ttk.Button(btn_box, text="✖ 取消", command=self._on_clear).pack(side="left", padx=4)

    def _pick_folder(self):
        d = filedialog.askdirectory(title="选择素材文件夹",
                                    initialdir=self.folder_var.get() or None)
        if d:
            self.folder_var.set(d)

    def _refresh_folder_count(self):
        path = self.folder_var.get().strip()
        if not path or not os.path.isdir(path):
            self.folder_count_lbl.configure(text="（未选）", foreground="#888")
            return
        try:
            count = sum(1 for f in os.listdir(path)
                        if f.lower().endswith((".mp4", ".mov", ".mkv", ".avi")))
            self.folder_count_lbl.configure(
                text=f"共 {count} 个视频", foreground="#1a73e8" if count else "#c00")
        except Exception:
            self.folder_count_lbl.configure(text="读取失败", foreground="#c00")

    def _refresh_pip_visibility(self):
        is_pip = "画中画" in self.layout_var.get()
        state = "normal" if is_pip else "disabled"
        self.pip_scale.configure(state=state)
        self.pip_lbl.configure(foreground=("#000" if is_pip else "#888"))

    # ---------- 数据加载/导出 ----------

    def show_insert_point(self, idx: int, ip: InsertPoint):
        """显示已有插入点（编辑模式）。"""
        self._current_idx = idx
        self._empty_label.pack_forget()
        self._form.pack(fill="both", expand=True)

        s_m, s_s, s_cs = self._sec_to_msc(ip.startTime)
        self.start_m.set(f"{s_m:02d}")
        self.start_s.set(f"{s_s:02d}")
        self.start_cs.set(f"{s_cs:02d}")

        if ip.endTime is None:
            self.end_unlimited_var.set(True)
            self.end_m.set("00")
            self.end_s.set("00")
            self.end_cs.set("00")
        else:
            self.end_unlimited_var.set(False)
            e_m, e_s, e_cs = self._sec_to_msc(ip.endTime)
            self.end_m.set(f"{e_m:02d}")
            self.end_s.set(f"{e_s:02d}")
            self.end_cs.set(f"{e_cs:02d}")

        self.folder_var.set(ip.folder or "")
        self.pick_mode_var.set("随机" if getattr(ip, "pickRandom", False) else "顺序")
        self.silent_var.set(bool(ip.silent))
        self.layout_var.set(LAYOUT_LABELS.get(ip.layout, LAYOUT_LABELS["replaced"]))
        self.pip_var.set(float(getattr(ip, "pipScale", 0.5) or 0.5))
        self.orient_var.set(ORIENTATION_LABELS.get(ip.orientation, ORIENTATION_LABELS["landscape"]))
        self.res_var.set(RESOLUTION_LABELS.get(ip.resolution, RESOLUTION_LABELS["720p"]))
        self.cleanup_var.set(CLEANUP_LABELS.get(ip.cleanupMode, CLEANUP_LABELS["archive_to_subfolder"]))
        self.subdir_var.set(getattr(ip, "subdirName", "used") or "used")
        self.note_var.set(getattr(ip, "note", "") or "")

        self._refresh_folder_count()
        self._refresh_pip_visibility()
        self.save_btn.configure(text="💾 保存修改")
        self.delete_btn.configure(state="normal")

    def show_new(self):
        """显示新增模式。"""
        self._current_idx = None
        self._empty_label.pack_forget()
        self._form.pack(fill="both", expand=True)

        self.start_m.set("00"); self.start_s.set("00"); self.start_cs.set("00")
        self.end_m.set("00"); self.end_s.set("05"); self.end_cs.set("00")
        self.end_unlimited_var.set(False)
        self.folder_var.set("")
        self.pick_mode_var.set("顺序")
        self.silent_var.set(False)
        self.layout_var.set(LAYOUT_LABELS["replaced"])
        self.pip_var.set(0.5)
        self.orient_var.set(ORIENTATION_LABELS["landscape"])
        self.res_var.set(RESOLUTION_LABELS["720p"])
        self.cleanup_var.set(CLEANUP_LABELS["archive_to_subfolder"])
        self.subdir_var.set("used")
        self.note_var.set("")

        self._refresh_folder_count()
        self._refresh_pip_visibility()
        self.save_btn.configure(text="➕ 添加")
        self.delete_btn.configure(state="disabled")

    def show_empty(self):
        """显示空状态。"""
        self._current_idx = None
        self._form.pack_forget()
        self._empty_label.pack(expand=True)

    def _save(self):
        ip = self._build_insert_point()
        if ip is None:
            return
        if self._current_idx is None:
            self._on_save(None, ip)  # 新增
        else:
            self._on_save(self._current_idx, ip)  # 编辑

    def _delete(self):
        if self._current_idx is None:
            return
        if messagebox.askyesno("确认", f"删除插入点 #{self._current_idx + 1}？"):
            self._on_delete(self._current_idx)

    def _build_insert_point(self) -> Optional[InsertPoint]:
        try:
            start = mmss_to_seconds(
                int(self.start_m.get() or 0),
                int(self.start_s.get() or 0),
                int(self.start_cs.get() or 0),
            )
        except Exception:
            messagebox.showerror("错误", "开始时间格式不对")
            return None

        if self.end_unlimited_var.get():
            end = None
        else:
            try:
                end = mmss_to_seconds(
                    int(self.end_m.get() or 0),
                    int(self.end_s.get() or 0),
                    int(self.end_cs.get() or 0),
                )
            except Exception:
                messagebox.showerror("错误", "结束时间格式不对")
                return None
            if end <= start:
                messagebox.showerror("错误", "结束时间必须大于开始时间")
                return None

        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showerror("错误", "请选择素材文件夹")
            return None
        if not os.path.isdir(folder):
            messagebox.showerror("错误", f"素材文件夹不存在：\n{folder}")
            return None

        # 反查 enum
        layout_en = next((k for k, v in LAYOUT_LABELS.items() if v == self.layout_var.get()),
                         "replaced")
        orient_en = next((k for k, v in ORIENTATION_LABELS.items() if v == self.orient_var.get()),
                         "landscape")
        res_en = next((k for k, v in RESOLUTION_LABELS.items() if v == self.res_var.get()),
                      "720p")
        cleanup_en = next((k for k, v in CLEANUP_LABELS.items() if v == self.cleanup_var.get()),
                          "archive_to_subfolder")

        return InsertPoint(
            startTime=start,
            endTime=end,
            folder=folder,
            videos=[],
            cleanupMode=cleanup_en,
            silent=bool(self.silent_var.get()),
            layout=layout_en,
            pipScale=float(self.pip_var.get()),
            orientation=orient_en,
            resolution=res_en,
            pickRandom=(self.pick_mode_var.get() == "随机"),
            subdirName=self.subdir_var.get().strip() or "used",
            note=self.note_var.get().strip(),
        )

    @staticmethod
    def _sec_to_msc(sec: float):
        total_cs = int(round(sec * 100))
        m = total_cs // 6000
        s = (total_cs // 100) % 60
        cs = total_cs % 100
        return m, s, cs


# ============================================================
# 主面板（Tab 5 整体）
# ============================================================

class MergeTabFrame(ttk.Frame):
    def __init__(self, parent, app=None):
        super().__init__(parent)
        self.app = app
        self.config: MixConfig = self._load_config()
        self._cards: list = []
        self._build()
        self._refresh_list()
        # ⭐ 关键：必须 pack 到父容器，否则 frame 是 1x1 看不到
        self.pack(fill="both", expand=True)

    # ---------- 配置持久化 ----------

    def _load_config(self) -> MixConfig:
        p = Path(CONFIG_FILE)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cfg = MixConfig.from_dict(data)
                return cfg
            except Exception:
                pass
        return MixConfig()

    def _save_config(self):
        try:
            Path(CONFIG_FILE).write_text(
                json.dumps(self.config.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            self._log(f"❌ 保存配置失败：{e}")

    # ---------- 构建 UI ----------

    def _build(self):
        # === 顶部固定：主视频 + 输出文件夹 === (grid row=0)
        top = ttk.LabelFrame(self, text="📁 主视频与输出", padding=6)
        top.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="主视频文件夹:").grid(row=0, column=0, sticky="w", padx=2)
        self.main_folder_var = tk.StringVar(value=self.config.mainFolder)
        ttk.Entry(top, textvariable=self.main_folder_var).grid(
            row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="📂 选择", command=self._pick_main).grid(row=0, column=2, padx=2)

        self.single_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="单选", variable=self.single_var,
                        command=self._refresh_video_list).grid(row=0, column=3, padx=8)
        self.main_video_var = tk.StringVar()
        self.main_video_combo = ttk.Combobox(top, textvariable=self.main_video_var,
                                             state="readonly", width=30)
        self.main_video_combo.grid(row=0, column=4, padx=2, sticky="we")

        ttk.Label(top, text="输出文件夹:").grid(row=1, column=0, sticky="w", padx=2, pady=(6, 0))
        self.output_folder_var = tk.StringVar(value=self.config.outputFolder)
        ttk.Entry(top, textvariable=self.output_folder_var).grid(
            row=1, column=1, sticky="we", padx=4, pady=(6, 0))
        ttk.Button(top, text="📂 选择", command=self._pick_output).grid(
            row=1, column=2, padx=2, pady=(6, 0))
        ttk.Button(top, text="↪ 同主视频", command=self._copy_main_to_output).grid(
            row=1, column=3, columnspan=2, padx=2, pady=(6, 0), sticky="w")

        # === ⭐ 关键：self 用 grid 三行布局，避免 pack 互相挤 ===
        # row 0 (weight=0): 顶部  - 主视频设置
        # row 1 (weight=1): 中间  - 左 40% / 右 60% (stretch)
        # row 2 (weight=0): 底部  - 输出与日志
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=0)  # top
        self.rowconfigure(1, weight=1)  # mid (主)
        self.rowconfigure(2, weight=0)  # bottom

        # === 中间主体：左 40% 列表 / 右 60% 编辑 ===
        mid = tk.Frame(self)
        mid.grid(row=1, column=0, sticky="nsew", padx=4, pady=2)

        # 左
        left_outer = tk.Frame(mid, bd=1, relief="solid")
        left_outer.pack(side="left", fill="both", expand=False)
        left_outer.configure(width=400)
        left_outer.pack_propagate(False)
        tk.Label(left_outer, text="📋 插入点列表", font=("Helvetica", 11, "bold"),
                 bg="#e0e0e0", anchor="w", padx=8, pady=4).pack(fill="x")
        toolbar = tk.Frame(left_outer)
        toolbar.pack(fill="x", pady=(4, 4), padx=4)
        ttk.Button(toolbar, text="➕ 添加插入点", command=self._on_add).pack(side="left", padx=2)
        ttk.Button(toolbar, text="💾 保存配置", command=self._save_config).pack(side="left", padx=2)
        ttk.Button(toolbar, text="📂 加载配置", command=self._reload_config).pack(side="left", padx=2)
        tk.Label(toolbar, text="💡 单击选中",
                 foreground="#888", font=("Helvetica", 9)).pack(side="left", padx=8)
        # 列表（用普通 tk.Frame，不用 Canvas/panedwindow）
        list_outer = tk.Frame(left_outer, bd=1, relief="sunken")
        list_outer.pack(fill="both", expand=True, padx=4, pady=4)
        self.list_canvas = tk.Canvas(list_outer, highlightthickness=0, bg="#fafafa")
        list_sb = ttk.Scrollbar(list_outer, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        list_sb.pack(side="right", fill="y")
        self.list_canvas.configure(yscrollcommand=list_sb.set)
        self.list_inner = tk.Frame(self.list_canvas, bg="#fafafa")
        self.list_window = self.list_canvas.create_window(
            (0, 0), window=self.list_inner, anchor="nw", tags="list_frame")
        self.list_inner.bind("<Configure>",
                             lambda _: self.list_canvas.configure(
                                 scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.bind("<Configure>",
                              lambda e: self.list_canvas.itemconfigure(
                                  "list_frame", width=e.width))

        # 分隔条（中间）
        splitter = tk.Frame(mid, width=4, bg="#cccccc", cursor="sb_h_double_arrow")
        splitter.pack(side="left", fill="y")

        # 右
        right_outer = tk.Frame(mid, bd=1, relief="solid")
        right_outer.pack(side="left", fill="both", expand=True)
        tk.Label(right_outer, text="📝 详情/编辑面板", font=("Helvetica", 11, "bold"),
                 bg="#e0e0e0", anchor="w", padx=8, pady=4).pack(fill="x")
        right_body = tk.Frame(right_outer)
        right_body.pack(fill="both", expand=True, padx=4, pady=4)
        self.editor = EditorPanel(
            right_body,
            on_save=self._on_save,
            on_delete=self._on_delete,
            on_clear=lambda: self.editor.show_empty(),
        )
        self.editor.pack(fill="both", expand=True)

        # === 底部固定：输出设置 + 按钮 + 日志 === (grid row=2)
        bottom = ttk.LabelFrame(self, text="🎬 输出与日志", padding=6)
        bottom.grid(row=2, column=0, sticky="ew", padx=4, pady=(2, 4))

        out_row = ttk.Frame(bottom)
        out_row.pack(fill="x")
        ttk.Label(out_row, text="文件名后缀:").pack(side="left")
        self.suffix_var = tk.StringVar(value=self.config.outputSuffix)
        ttk.Entry(out_row, textvariable=self.suffix_var, width=12).pack(side="left", padx=4)
        ttk.Label(out_row, text="  横竖屏:").pack(side="left")
        self.orient_out_var = tk.StringVar(
            value=ORIENTATION_LABELS.get(self.config.outputSettings.orientation,
                                         ORIENTATION_LABELS["landscape"]))
        ttk.Combobox(out_row, textvariable=self.orient_out_var,
                     values=[ORIENTATION_LABELS[x] for x in ORIENTATION_OPTIONS],
                     state="readonly", width=12).pack(side="left", padx=4)
        ttk.Label(out_row, text="  分辨率:").pack(side="left")
        self.res_out_var = tk.StringVar(
            value=RESOLUTION_LABELS.get(self.config.outputSettings.resolution,
                                        RESOLUTION_LABELS["720p"]))
        ttk.Combobox(out_row, textvariable=self.res_out_var,
                     values=[RESOLUTION_LABELS[x] for x in RESOLUTION_OPTIONS],
                     state="readonly", width=12).pack(side="left", padx=4)
        ttk.Label(out_row, text="  主视频清理:").pack(side="left", padx=(20, 0))
        self.main_cleanup_var = tk.StringVar(
            value=CLEANUP_LABELS.get(self.config.mainCleanupMode,
                                     CLEANUP_LABELS["archive_to_subfolder"]))
        ttk.Combobox(out_row, textvariable=self.main_cleanup_var,
                     values=[CLEANUP_LABELS[x] for x in CLEANUP_OPTIONS],
                     state="readonly", width=20).pack(side="left", padx=4)

        # 按钮
        btn_row = ttk.Frame(bottom)
        btn_row.pack(fill="x", pady=(6, 0))
        self.start_btn = tk.Button(
            btn_row, text="🚀  开 始 混 剪",
            command=self._on_start,
            bg="#2d7d46", fg="white",
            font=("Helvetica", 14, "bold"),
            relief="raised", bd=2, height=2,
        )
        self.start_btn.pack(side="left", fill="x", expand=True, padx=4)
        self.stop_btn = tk.Button(
            btn_row, text="⏹ 停止",
            command=self._on_stop, state="disabled",
            bg="#a04040", fg="white",
            font=("Helvetica", 12),
        )
        self.stop_btn.pack(side="left", padx=4)
        self.progress_lbl = ttk.Label(btn_row, text="", font=("Helvetica", 11))
        self.progress_lbl.pack(side="left", padx=10)

        # 日志
        log_frame = ttk.Frame(bottom)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        ttk.Label(log_frame, text="📋 运行日志:").pack(side="left")
        ttk.Button(log_frame, text="🗑 清空", command=self._clear_log).pack(side="right")
        self.log_text = tk.Text(
            log_frame, height=6, wrap="word",
            font=("Menlo", 10), bg="#1e1e1e", fg="#e0e0e0",
        )
        self.log_text.pack(fill="both", expand=True, pady=(2, 0))
        self.log_text.configure(state="disabled")
        log_sb = ttk.Scrollbar(self.log_text, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")

    # ---------- 列表刷新 ----------

    def _refresh_list(self):
        for w in self.list_inner.winfo_children():
            w.destroy()
        self._cards = []
        if not self.config.insertPoints:
            ttk.Label(self.list_inner, text="（暂无插入点）",
                      foreground="#888", padding=20).pack(fill="x")
            self.editor.show_empty()
            return
        for i, ip in enumerate(self.config.insertPoints):
            card = SegmentCard(
                self.list_inner, i, ip,
                on_click=self._on_select,
                on_double_click=self._on_edit,
            )
            card.pack(fill="x", padx=2, pady=2)
            self._cards.append(card)
        if self._cards:
            self._on_select(0)

    def _on_select(self, idx: int):
        for i, c in enumerate(self._cards):
            c.set_selected(i == idx)
        if 0 <= idx < len(self.config.insertPoints):
            self.editor.show_insert_point(idx, self.config.insertPoints[idx])

    def _on_edit(self, idx: int):
        self._on_select(idx)

    def _on_add(self):
        self.editor.show_new()
        for c in self._cards:
            c.set_selected(False)

    def _on_save(self, idx, ip):
        if idx is None:
            self.config.insertPoints.append(ip)
        else:
            self.config.insertPoints[idx] = ip
        self._save_config()
        self._refresh_list()
        new_idx = len(self.config.insertPoints) - 1 if idx is None else idx
        if self._cards:
            self._on_select(new_idx)
        self._log(f"✅ 已保存插入点 #{new_idx + 1}")

    def _on_delete(self, idx):
        if 0 <= idx < len(self.config.insertPoints):
            del self.config.insertPoints[idx]
            self._save_config()
            self._refresh_list()
            self._log(f"🗑 已删除插入点 #{idx + 1}")

    def _reload_config(self):
        self.config = self._load_config()
        self._refresh_list()
        self._log("📂 已重新加载配置")

    # ---------- 文件夹选择 ----------

    def _pick_main(self):
        d = filedialog.askdirectory(title="选择主视频文件夹",
                                    initialdir=self.main_folder_var.get() or None)
        if d:
            self.main_folder_var.set(d)
            self._refresh_video_list()

    def _pick_output(self):
        d = filedialog.askdirectory(title="选择输出文件夹",
                                    initialdir=self.output_folder_var.get() or None)
        if d:
            self.output_folder_var.set(d)

    def _copy_main_to_output(self):
        if self.main_folder_var.get():
            self.output_folder_var.set(self.main_folder_var.get())

    def _refresh_video_list(self):
        folder = self.main_folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            self.main_video_combo.configure(values=[])
            return
        try:
            vids = sorted(
                f for f in os.listdir(folder)
                if f.lower().endswith((".mp4", ".mov", ".mkv", ".avi"))
            )
        except Exception:
            vids = []
        self.main_video_combo.configure(values=vids)
        if vids and self.single_var.get():
            self.main_video_var.set(vids[0])

    # ---------- 日志 ----------

    def _log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ---------- 开始/停止 ----------

    def _collect_config(self) -> Optional[MixConfig]:
        main_folder = self.main_folder_var.get().strip()
        if not main_folder:
            messagebox.showerror("错误", "请选择主视频文件夹")
            return None
        output_folder = self.output_folder_var.get().strip()
        if not output_folder:
            messagebox.showerror("错误", "请选择输出文件夹")
            return None
        if not self.config.insertPoints:
            messagebox.showerror("错误", "请至少添加一个插入点")
            return None

        if self.single_var.get():
            main_videos = [self.main_video_var.get()] if self.main_video_var.get() else []
            if not main_videos:
                messagebox.showerror("错误", "单选模式请先选一个主视频")
                return None
        else:
            folder = main_folder
            if not os.path.isdir(folder):
                messagebox.showerror("错误", "主视频文件夹不存在")
                return None
            main_videos = sorted(
                os.path.join(folder, f) for f in os.listdir(folder)
                if f.lower().endswith((".mp4", ".mov", ".mkv", ".avi"))
            )
            if not main_videos:
                messagebox.showerror("错误", "主视频文件夹里没有视频文件")
                return None

        orient_en = next((k for k, v in ORIENTATION_LABELS.items()
                          if v == self.orient_out_var.get()), "landscape")
        res_en = next((k for k, v in RESOLUTION_LABELS.items()
                       if v == self.res_out_var.get()), "720p")
        cleanup_en = next((k for k, v in CLEANUP_LABELS.items()
                           if v == self.main_cleanup_var.get()), "archive_to_subfolder")

        return MixConfig(
            mainFolder=main_folder,
            mainVideos=main_videos,
            insertPoints=list(self.config.insertPoints),
            outputFolder=output_folder,
            outputSuffix=self.suffix_var.get().strip() or "_混剪",
            outputSettings=OutputSettings(orientation=orient_en, resolution=res_en),
            mainCleanupMode=cleanup_en,
        )

    def _on_start(self):
        cfg = self._collect_config()
        if cfg is None:
            return
        self._save_config()
        self._set_state(running=True)
        self._clear_log()
        self._log(f"🚀 开始混剪：{len(cfg.mainVideos)} 个主视频 × {len(cfg.insertPoints)} 个插入点")

        def on_log(msg):
            self.after(0, lambda: self._log(msg))

        def on_finished(success: bool):
            self.after(0, lambda: self._on_finished(success))

        self._scheduler = MixScheduler(
            cfg, log_callback=on_log, on_finished=on_finished,
        )
        self._scheduler.start()

    def _on_stop(self):
        if hasattr(self, "_scheduler") and self._scheduler:
            self._scheduler.stop()
            self._log("⏹ 停止请求已发送…")

    def _on_finished(self, success: bool):
        self._set_state(running=False)
        if success:
            self.progress_lbl.configure(text="✅ 完成", foreground="#2d7d46")
            self._log("✅ 全部完成")
        else:
            self.progress_lbl.configure(text="❌ 失败（看日志）", foreground="#c00")

    def _set_state(self, running: bool):
        if running:
            self.start_btn.configure(state="disabled", bg="#888")
            self.stop_btn.configure(state="normal")
            self.progress_lbl.configure(text="⏳ 跑中…", foreground="#1a73e8")
        else:
            self.start_btn.configure(state="normal", bg="#2d7d46")
            self.stop_btn.configure(state="disabled")
