"""
ai_tab.py - v1.18 Tab 6「🤖 ai智能」

完整流程：
  视频文件夹 + 素材文件夹列表（带秒数/必插/标签）
  → 选预设模式 + 意图
  → 配置执行参数（沿用给 Tab 5）
  → 批量 ASR 字幕（v1.14 真实调硅基流动） + LLM 决定插入点（v1.14 真实调 Qwen）
  → 审核插入点（可改时间/位置/mode）
  → 推 Tab 5 跑混剪

v1.13 阶段 1：UI 骨架 + mock
v1.14 阶段 2：接真实 ASR/LLM（ai_clients.py），删气口环节已移除
v1.15 阶段 3：Tab 5 _on_start 修 pipMargin/cleanup_mode + AI exec_config 覆盖
v1.16 阶段 4：并发批量分析（worker pool，可配并发数 1~10）
v1.17 阶段 5：LLM 强制 JSON 输出（response_format）+ parse 兜底（容错 markdown/混文本）
v1.18 阶段 6：兼容 SSE 流式响应（本地 vllm）+ prompt 改英文+简洁版（Kimi 本地模型听话）
"""
import os
import re
import json
import math
import time
import random
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from typing import List, Dict, Optional, Callable

from ai_clients import ASRClient, LLMClient, parse_llm_json

# v1.12/v1.14 默认值（与 main.py 一致；UI 未填时回落）
ASR_DEFAULT_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_DEFAULT_MODEL = "TeleAI/TeleSpeechASR"
LLM_DEFAULT_URL = "https://api.siliconflow.cn/v1/chat/completions"
LLM_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 8192

LAYOUT_OPTIONS_DISPLAY = [
    ("replaced",         "替换主材画面（铺满）"),
    ("pip_center",       "画中画 居中"),
    ("pip_top_left",     "画中画 左上角"),
    ("pip_top_right",    "画中画 右上角"),
    ("pip_bottom_left",  "画中画 左下角"),
    ("pip_bottom_right", "画中画 右下角"),
]
LAYOUT_DISPLAY = {k: v for k, v in LAYOUT_OPTIONS_DISPLAY}

MODE_OPTIONS_DISPLAY = [
    ("small", "小插入（1段素材）"),
    ("large", "大插入（跨多段）"),
]
MODE_DISPLAY = {k: v for k, v in MODE_OPTIONS_DISPLAY}

CLEANUP_OPTIONS_DISPLAY = [
    ("move",  "移到子文件夹"),
    ("trash", "删除到回收站"),
    ("keep",  "不做处理"),
]

EXTRACT_OPTIONS_DISPLAY = [
    ("random",     "🎲 随机"),
    ("sequential", "顺序"),
]

# v2.10.14 新增：输出分辨率（与 merge_tab.py RESOLUTION_OPTIONS 同步）
# 默认 1080P (符合短视频平台主流规格 + 房产素材通常 1080p)
RESOLUTION_OPTIONS_DISPLAY = [
    ("720p",  "📺 720P (高清)"),
    ("1080p", "📺 1080P (超清)"),
]

# v2.10 已删除：插入策略（v1.22 三种口播模板）/ 密度档（v1.83 4 档 + 自定义）
# 这些在 v2.10c 算法里都不再用，由"覆盖模式 + 素材占比"完全替代

# v1.47：所有 *_KEY_BY_DISPLAY 反向字典（display 中文名 → key 英文 key），统一放在这里
EXTRACT_OPTIONS_KEY_BY_DISPLAY = {v: k for k, v in EXTRACT_OPTIONS_DISPLAY}
LAYOUT_KEY_BY_DISPLAY = {v: k for k, v in LAYOUT_OPTIONS_DISPLAY}
CLEANUP_KEY_BY_DISPLAY = {v: k for k, v in CLEANUP_OPTIONS_DISPLAY}
RESOLUTION_KEY_BY_DISPLAY = {v: k for k, v in RESOLUTION_OPTIONS_DISPLAY}

# v2.10.10 已删除：PRESET_MODES 常量（4 选 1 radio）
# v2.10 改用"覆盖模式 + 素材占比"取代之（_build_section_mode 里现存的 🌐 自由 / 📍 双端固定 radios）

# v2.10 已删除：v1.83 4 档密度档位（X = 每 X 秒 1 个插入点）
# DENSITY_LEVELS = [...] （已废弃）
# DENSITY_DEFAULT = "medium"（已废弃）

VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv")


class AITab(tk.Frame):
    """Tab 6: 🤖 ai智能"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.root = app.root if hasattr(app, 'root') else parent.winfo_toplevel()

        # ========== 状态变量 ==========
        # 1️⃣ 输入段
        # v1.47：从 config 读取上次的主视频文件夹
        video_folder_default = ""
        if self.app is not None and hasattr(self.app, "config"):
            video_folder_default = self.app.config.get("defaults", {}).get("video_folder", "")
        self.video_folder_var = tk.StringVar(value=video_folder_default)
        self.video_list = []  # 视频文件夹下的 .mp4 等文件列表
        self.material_folders = []  # [{enabled, path, label, seconds, required}]
        # v1.47：从 config 读取上次的素材文件夹列表
        if self.app is not None and hasattr(self.app, "config"):
            saved_folders = self.app.config.get("defaults", {}).get("material_folders", [])
            for mf in saved_folders:
                if isinstance(mf, dict) and mf.get("path"):
                    self.material_folders.append({
                        "enabled": bool(mf.get("enabled", True)),
                        "path": mf.get("path", ""),
                        "label": mf.get("label", "") or os.path.basename(mf.get("path", "")),
                        "seconds": int(mf.get("seconds", 5)),
                        "required": bool(mf.get("required", False)),
                    })
        # v1.47：trace video_folder_var 变化时持久化
        self._trace_video_folder()
        # v2.10.26: 4 个覆盖率 var 加 trace 自动持久化（必须在 var 初始化之后调用）
        # 实际调用移到 L146 之后
        # v1.47：渲染已加载的素材文件夹到 UI（否则即使内存有数据，UI 也是空白）
        # 必须在 UI 组件（mat_inner）创建之后，所以延后到 build_section_input 跑完
        # 简单办法：用 root.after 延迟到整个 tab UI 构建完成后
        try:
            self.root.after(200, self._refresh_material_list)
        except Exception as e:
            print(f"[ai_tab.init] refresh material list 失败：{e}")

        # 2️⃣ 模式 + 意图
        # v2.10.10 已删除：preset_mode_var (4 选 1) + fixed_count_var (N 框) + insert_intent_var (插入意图)
        # 改用 v2.10 的"覆盖模式 + 素材占比"取代之

        # v2.10 新增：素材覆盖率（用户自定 0-100%）
        # 0% = 纯主播；50% = 半素材半主播；100% = 纯素材
        # v2.10.26: 从 self.app.config 读持久化值，找不到用默认 50.0
        _defaults = self.app.config.get("defaults", {}) if self.app is not None and hasattr(self.app, "config") else {}
        self.mat_pct_var = tk.DoubleVar(value=_defaults.get("mat_pct", 50.0))
        # v2.10 新增：覆盖模式（"free"=自由分布；"center"=双端固定，素材集中在中间）
        self.coverage_mode_var = tk.StringVar(value=_defaults.get("coverage_mode", "free"))
        # v2.10.24 新增：覆盖率约束总开关（关掉时不传 mat_pct/coverage_mode 给 LLM，回 v2.10.22 自由模式）
        self.enable_coverage_var = tk.BooleanVar(value=_defaults.get("enable_coverage", True))
        # v2.10.25 新增：第二层审核开关（关掉时不调用第二层 LLM，第一层结果直接用）
        self.second_review_var = tk.BooleanVar(value=_defaults.get("second_review", True))
        # v2.10.34 新增：单次批量执行上限（视频文件夹有 50 条，设 10 = 只跑前 10 条，剩 40 条下次再跑）
        self.max_batch_per_run_var = tk.IntVar(value=int(_defaults.get("max_batch_per_run", 10)))

        # v2.10.26: 4 个 var 都初始化完后再加 trace 自动持久化（必须在 var 创建之后调用）
        # v2.10.34: 加第 5 个 _trace_max_batch_per_run
        self._trace_mat_pct()
        self._trace_coverage_mode()
        self._trace_enable_coverage()
        self._trace_second_review()
        self._trace_max_batch_per_run()

        # 3️⃣ 执行配置
        # v2.10.40: 从 config.yaml defaults 读 extract_mode（持久化），找不到默认 "🎲 随机"
        self.extract_mode_var = tk.StringVar(value=_defaults.get("extract_mode", "🎲 随机"))  # v1.47: 中文 display
        # v2.10.40: extract_mode_var 定义后才能调 _trace_extract_mode
        self._trace_extract_mode()
        self.mute_material_var = tk.BooleanVar(value=True)
        self.layout_var = tk.StringVar(value="替换主材画面（铺满）")  # v1.47: 中文 display
        # v2.10.14 新增：输出分辨率（推到 Tab 5, 默认 1080P）
        self.output_resolution_var = tk.StringVar(value="📺 1080P (超清)")
        # v2.10 已删除：插入策略（v1.22 free/sandwich/two_anchor）由"覆盖模式"完全替代

        # v1.24 视频行业（让 LLM 知道方向，识别字幕/选素材不跑偏）
        industry_default = "通用"
        if self.app is not None and hasattr(self.app, "config"):
            industry_default = self.app.config.get("defaults", {}).get("industry", industry_default)
        self.industry_var = tk.StringVar(value=industry_default)
        self._trace_industry()
        self.pip_size_var = tk.IntVar(value=50)
        self.pip_margin_var = tk.IntVar(value=20)
        self.cleanup_mode_var = tk.StringVar(value="移到子文件夹")  # v1.47: 中文 display
        self.main_video_cleanup_var = tk.StringVar(value="移到子文件夹")  # v1.47: 改成中文 display，跟 Combobox 一致；推到 tab5 才能匹配 CLEANUP_DISPLAY.values()
        self.auto_review_var = tk.BooleanVar(value=False)

        # v1.21：AI 批量执行输出目录（独立于手动混剪）
        ai_default = "/Users/zxz/Desktop/口播成品"
        if self.app is not None and hasattr(self.app, "config"):
            ai_default = self.app.config.get("defaults", {}).get("ai_output_dir", ai_default)
        self.ai_output_dir = tk.StringVar(value=ai_default)
        self._trace_ai_output_dir()

        # 4️⃣ 启动（v1.13 删气口功能已移除，不再需要 silence_threshold_var）

        # 5️⃣ 队列
        self.queue = []  # [{video_path, status, subtitles, insertions, materials, ...}]
        self.is_running = False
        self.active_workers = 0  # v1.16 并发：当前在跑的 worker 数
        self.current_review_video = None  # 当前展开审核的视频路径

        # v1.41：跨视频素材去重（防止同一素材被多条主视频复用）
        import threading as _thr
        self._used_material_paths: set = set()
        self._extract_lock = _thr.Lock()

        # ========== v1.13: Canvas + Scrollbar 包裹整个 Tab（6 段太高，需要滚动）==========
        self._outer_canvas = tk.Canvas(self, highlightthickness=0)
        self._outer_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._outer_scroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._outer_canvas.yview)
        self._outer_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._outer_canvas.configure(yscrollcommand=self._outer_scroll.set)

        self.body = ttk.Frame(self._outer_canvas)
        self._body_window = self._outer_canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self._outer_canvas.configure(scrollregion=self._outer_canvas.bbox("all")))
        self._outer_canvas.bind("<Configure>", lambda e: self._outer_canvas.itemconfig(self._body_window, width=e.width))

        def _on_outer_mw(e):
            if e.delta:
                self._outer_canvas.yview_scroll(int(-e.delta), "units")
        self._outer_canvas.bind("<Enter>", lambda e: self._outer_canvas.bind_all("<MouseWheel>", _on_outer_mw))
        self._outer_canvas.bind("<Leave>", lambda e: self._outer_canvas.unbind_all("<MouseWheel>"))

        # ========== 6 段构建（全部挂到 self.body 而不是 self）==========
        self._build_section_input()
        self._build_section_mode()
        self._build_section_exec()
        self._build_section_run()
        self._build_section_queue()

        # v1.47：init 后渲染已加载的素材文件夹列表（否则 UI 上看不到）
        try:
            self._refresh_material_list()
        except Exception:
            pass

        # 必须 pack 自己，否则 Tab 内容显示不出来（v1.13 修复）
        self.pack(fill="both", expand=True)

    # ================================================================
    # 1️⃣ 输入段：视频文件夹 + 素材文件夹列表
    # ================================================================
    def _build_section_input(self):
        f = ttk.LabelFrame(self.body, text="1️⃣ 输入", padding=8)
        f.pack(fill=tk.X, pady=5)
        f.columnconfigure(1, weight=1)

        # 视频文件夹
        ttk.Label(f, text="视频文件夹:").grid(row=0, column=0, sticky=tk.W, pady=3)
        vf_row = ttk.Frame(f)
        vf_row.grid(row=0, column=1, sticky=tk.EW, pady=3)
        ttk.Entry(vf_row, textvariable=self.video_folder_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(vf_row, text="📁 浏览", command=self._on_browse_video_folder).pack(side=tk.LEFT, padx=2)

        # v1.40：视频数（左边） + 主视频用过处理（右边） 同一行
        info_row = ttk.Frame(f)
        info_row.grid(row=1, column=1, sticky=tk.EW, pady=2)
        self.video_count_label = ttk.Label(info_row, text="（未选文件夹）", foreground="#888")
        self.video_count_label.pack(side=tk.LEFT)
        ttk.Label(info_row, text="  │  用过处理:").pack(side=tk.LEFT, padx=(8, 4))
        ttk.Combobox(info_row, textvariable=self.main_video_cleanup_var, width=12,
                     values=[v for _, v in CLEANUP_OPTIONS_DISPLAY], state="readonly"
        ).pack(side=tk.LEFT)
        ttk.Label(info_row, text="（推到 Tab 5 后执行）", foreground="#888", font=("", 8)).pack(side=tk.LEFT, padx=4)
        # v2.10.34：单次批量执行上限（紧跟用过处理 Combobox 后面；Spinbox 偶发返回空 → 兜底 0=不限制）
        # 视频文件夹有 50 条，设 10 = 点「批量推 Tab 5 并执行」只跑前 10 条，剩 40 条下次再跑
        ttk.Label(info_row, text="  │  单次上限:").pack(side=tk.LEFT, padx=(8, 4))
        self.max_batch_spin = ttk.Spinbox(
            info_row, from_=0, to=9999, width=6,
            textvariable=self.max_batch_per_run_var,
        )
        self.max_batch_spin.pack(side=tk.LEFT)
        ttk.Label(info_row, text="条（0=不限制）", foreground="#888", font=("", 8)).pack(side=tk.LEFT, padx=(2, 0))
        # v2.10.35：输出分辨率（全局结果参数，跟单次上限同一行）
        # 之前放在「3️⃣ 执行配置」row 0 紧贴「抽取方式」，让用户误以为是插入素材参数，已搬走
        ttk.Label(info_row, text="  │  输出分辨率:").pack(side=tk.LEFT, padx=(8, 4))
        self.output_resolution_combo = ttk.Combobox(
            info_row,
            textvariable=self.output_resolution_var,
            values=[v for _, v in RESOLUTION_OPTIONS_DISPLAY],
            state="readonly", width=16,
        )
        self.output_resolution_combo.pack(side=tk.LEFT)

        # 素材文件夹
        ttk.Label(f, text="素材文件夹:").grid(row=2, column=0, sticky=tk.NW, pady=(8, 3))

        mat_outer = ttk.Frame(f)
        mat_outer.grid(row=2, column=1, sticky=tk.EW, pady=(8, 3))
        mat_outer.columnconfigure(0, weight=1)

        # 列表区
        list_wrap = ttk.Frame(mat_outer)
        list_wrap.grid(row=0, column=0, sticky=tk.EW)
        list_wrap.columnconfigure(0, weight=1)

        self.mat_canvas = tk.Canvas(list_wrap, height=120, highlightthickness=1, highlightbackground="#ddd")
        self.mat_scroll = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=self.mat_canvas.yview)
        self.mat_inner = ttk.Frame(self.mat_canvas)
        self.mat_inner.bind("<Configure>", lambda e: self.mat_canvas.configure(scrollregion=self.mat_canvas.bbox("all")))
        self._mat_win = self.mat_canvas.create_window((0, 0), window=self.mat_inner, anchor="nw")
        self.mat_canvas.bind("<Configure>", lambda e: self.mat_canvas.itemconfig(self._mat_win, width=e.width))
        self.mat_canvas.configure(yscrollcommand=self.mat_scroll.set)
        self.mat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.mat_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 鼠标滚轮
        def _on_wheel(e):
            if e.delta:
                self.mat_canvas.yview_scroll(int(-e.delta), "units")
        self.mat_canvas.bind("<Enter>", lambda e: self.mat_canvas.bind_all("<MouseWheel>", _on_wheel))
        self.mat_canvas.bind("<Leave>", lambda e: self.mat_canvas.unbind_all("<MouseWheel>"))

        # 表头
        self._render_material_header()

        # 添加按钮
        btn_row = ttk.Frame(mat_outer)
        btn_row.grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Button(btn_row, text="➕ 添加文件夹", command=self._on_add_material_folder).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="➖ 删除选中", command=self._on_remove_material_folder).pack(side=tk.LEFT, padx=2)
        # v2.09：手动刷新按钮（数 USB 真实剩余 mp4/mov/mkv，跳过 `._` AppleDouble）
        ttk.Button(btn_row, text="🔄 刷新剩余", command=self._on_refresh_remain_count).pack(side=tk.LEFT, padx=2)

        # 提示
        ttk.Label(mat_outer, text="💡 标签给 LLM 看 / 秒数让 LLM 选合适窗 / 必插在模式 2 时生效",
                  foreground="#888", font=("", 8)).grid(row=2, column=0, sticky=tk.W, pady=(4, 0))

    def _render_material_header(self):
        # 清掉旧表头（如果存在）
        if hasattr(self, 'mat_header_frame'):
            try:
                for w in self.mat_header_frame.winfo_children():
                    w.destroy()
                self.mat_header_frame.destroy()
            except tk.TclError:
                pass
        # 重建
        self.mat_header_frame = ttk.Frame(self.mat_inner)
        self.mat_header_frame.pack(fill=tk.X, pady=(0, 2))
        # v2.08：6 列：启用/路径/标签/秒数/必插/剩余
        ttk.Label(self.mat_header_frame, text="启用", width=5).grid(row=0, column=0, padx=2)
        ttk.Label(self.mat_header_frame, text="路径", width=30).grid(row=0, column=1, padx=2, sticky=tk.W)
        ttk.Label(self.mat_header_frame, text="标签", width=10).grid(row=0, column=2, padx=2)
        ttk.Label(self.mat_header_frame, text="秒数", width=6).grid(row=0, column=3, padx=2)
        ttk.Label(self.mat_header_frame, text="必插", width=5).grid(row=0, column=4, padx=2)
        ttk.Label(self.mat_header_frame, text="剩余", width=6, foreground="#888").grid(row=0, column=5, padx=2)

    def _refresh_material_list(self):
        """重建素材文件夹行 UI（启用/标签/秒数/必插）"""
        # 清掉所有行（保留表头）
        for child in list(self.mat_inner.winfo_children()):
            if child != getattr(self, 'mat_header_frame', None):
                child.destroy()

        # 移除旧表头再重新渲染（在最顶端）
        if hasattr(self, 'mat_header_frame'):
            self.mat_header_frame.destroy()

        # v2.10.10 已删除：preset_mode 4 选 1 联动逻辑（必插强制/禁用）
        # v2.10 起由"覆盖模式 + 素材占比"统一管理，必插列永远可勾（由 LLM 自觉保证种类覆盖）
        forced_required = False
        disable_required = False

        self._render_material_header()

        for idx, mf in enumerate(self.material_folders):
            row = ttk.Frame(self.mat_inner)
            row.pack(fill=tk.X, pady=1)

            # 启用
            enabled_var = tk.BooleanVar(value=mf["enabled"])
            ttk.Checkbutton(row, variable=enabled_var,
                            command=lambda i=idx, v=enabled_var: self._on_mat_enabled_change(i, v)).grid(row=0, column=0, padx=2)

            # 路径
            ttk.Label(row, text=os.path.basename(mf["path"]) or mf["path"], width=30, anchor="w").grid(row=0, column=1, padx=2, sticky=tk.W)

            # 标签
            label_var = tk.StringVar(value=mf["label"])
            label_entry = ttk.Entry(row, textvariable=label_var, width=10)
            label_entry.grid(row=0, column=2, padx=2)
            label_var.trace_add("write", lambda *a, i=idx, v=label_var: (self.material_folders[i].update({"label": v.get()}), self._save_material_folders()))

            # 秒数
            sec_var = tk.IntVar(value=mf["seconds"])
            sec_entry = ttk.Entry(row, textvariable=sec_var, width=6)
            sec_entry.grid(row=0, column=3, padx=2)
            sec_var.trace_add("write", lambda *a, i=idx, v=sec_var: (self._safe_set_int(v, self.material_folders[i], "seconds"), self._save_material_folders()))

            # 必插
            req_var = tk.BooleanVar(value=mf["required"])
            req_state = "disabled" if disable_required else ("selected" if forced_required else "!disabled")
            # 模式 1 强制勾选：直接用 ttk.Checkbutton 但 state 禁用；保存时强制设回 True
            req_cb = ttk.Checkbutton(row, variable=req_var,
                                     command=lambda i=idx, v=req_var: (self.material_folders[i].update({"required": v.get()}), self._save_material_folders()))
            if forced_required:
                req_var.set(True)
                req_cb.state(["disabled"])
            elif disable_required:
                req_cb.state(["disabled"])
            req_cb.grid(row=0, column=4, padx=2)

            # v2.08：剩余素材数（实时数 mp4/mov/mkv/avi/m4v/webm，跳过 `._` AppleDouble）
            remain = self._count_remaining_videos(mf["path"])
            remain_text = f"{remain}" if remain > 0 else "0"
            remain_color = "#cc0000" if remain == 0 else ("#cc8800" if remain < 3 else "#444")
            ttk.Label(row, text=remain_text, width=6, foreground=remain_color, anchor="center").grid(row=0, column=5, padx=2)

            # 删除按钮（用 index）
            ttk.Button(row, text="🗑", width=3,
                       command=lambda i=idx: self._on_remove_material_folder_at(i)).grid(row=0, column=6, padx=2)

    def _on_mat_enabled_change(self, idx, var):
        self.material_folders[idx]["enabled"] = var.get()
        self._save_material_folders()

    def _on_refresh_remain_count(self):
        """v2.09：手动触发重算每个素材文件夹的剩余素材数 + 写日志"""
        n_folders = len(self.material_folders)
        if n_folders == 0:
            self._log("[🔄 刷新剩余] 当前没有任何素材文件夹")
            return

        self._refresh_material_list()

        zero = sum(1 for mf in self.material_folders if self._count_remaining_videos(mf["path"]) == 0)
        low  = sum(1 for mf in self.material_folders
                   if 0 < self._count_remaining_videos(mf["path"]) < 3)
        ok   = n_folders - zero - low

        summary = (
            f"[🔄 刷新剩余] 共 {n_folders} 个文件夹，"
            f"🟢 充足 {ok} / 🟠 偏少 {low} / 🔴 为 0 {zero}"
        )
        self._log(summary)
        for mf in self.material_folders:
            n = self._count_remaining_videos(mf["path"])
            tag = "🔴" if n == 0 else ("🟠" if n < 3 else "🟢")
            self._log(f"  {tag} [{mf['label']}] {os.path.basename(mf['path'])} → 剩余 {n}")

    def _safe_set_int(self, var, target, key):
        try:
            target[key] = var.get()
        except (tk.TclError, ValueError):
            pass

    def _on_browse_video_folder(self):
        folder = filedialog.askdirectory(title="选择视频文件夹（批量）", parent=self)
        if not folder:
            return
        self.video_folder_var.set(folder)
        self._refresh_video_list()

    def _refresh_video_list(self):
        folder = self.video_folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            self.video_list = []
            self.video_count_label.config(text="（未选文件夹）", foreground="#888")
            return
        try:
            files = [f for f in os.listdir(folder)
                     if f.lower().endswith(VIDEO_EXTS)
                     and not f.startswith("._")
                     and os.path.isfile(os.path.join(folder, f))]
            files.sort()
        except Exception as e:
            self.video_list = []
            self.video_count_label.config(text=f"❌ 读取失败: {e}", foreground="red")
            return
        self.video_list = [os.path.join(folder, f) for f in files]
        n = len(self.video_list)
        self.video_count_label.config(
            text=f"✅ 已识别 {n} 个视频" + (f"   首文件: {os.path.basename(self.video_list[0])}" if n else ""),
            foreground="#4a90d9" if n else "#888"
        )

    def _on_add_material_folder(self):
        folder = filedialog.askdirectory(title="选择素材文件夹", parent=self)
        if not folder:
            return
        # 默认同名标签
        default_label = os.path.basename(folder)
        # 默认必插 = False
        self.material_folders.append({
            "enabled": True,
            "path": folder,
            "label": default_label,
            "seconds": 5,
            "required": False,
        })
        # 自动同步 fixed_count_var 默认值
        # v2.10.10 已删除：自动同步 fixed_count_var 默认值（preset_mode 4 选 1 已删除）
        self._refresh_material_list()
        self._save_material_folders()  # v1.47 持久化

    def _on_remove_material_folder(self):
        """删除最后一行（简化版，不做选中）"""
        if not self.material_folders:
            return
        self.material_folders.pop()
        self._refresh_material_list()
        self._save_material_folders()  # v1.47 持久化

    def _on_remove_material_folder_at(self, idx):
        if 0 <= idx < len(self.material_folders):
            self.material_folders.pop(idx)
            self._refresh_material_list()
            self._save_material_folders()  # v2.10.56 修复：之前没存 → 下次启动配置里还有这个文件夹

    # ================================================================
    # 2️⃣ 模式 + 意图
    # ================================================================
    def _build_section_mode(self):
        f = ttk.LabelFrame(self.body, text="2️⃣ 视频行业 + 覆盖策略", padding=8)
        f.pack(fill=tk.X, pady=5)
        f.columnconfigure(1, weight=1)

        # v2.10.10 已删除：
        # - 预设模式 4 选 1 radio (preset_mode_var)
        # - N 框 Spinbox (fixed_count_var)
        # - 插入意图 Entry (insert_intent_var)
        # 全部由 v2.10 覆盖模式 + 素材占比取代

        # v1.26 视频行业：给 LLM 看的上下文（不放 Tab 5 沿用区）
        ttk.Label(f, text="视频行业:").grid(row=0, column=0, sticky=tk.W, pady=3)
        industry_cell = ttk.Frame(f)
        industry_cell.grid(row=0, column=1, sticky=tk.EW, pady=3)
        industry_cell.columnconfigure(0, weight=1)
        ttk.Entry(industry_cell, textvariable=self.industry_var).grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(industry_cell, text="↳ 自填行业关键词，例如 '南宁房产'。留空 = 通用（不传给 LLM）",
                  foreground="#888", font=("", 8)).grid(row=1, column=0, sticky=tk.W)

        # ──────── v2.10 覆盖模式 + 素材占比 ────────
        coverage_sep = ttk.Separator(f, orient=tk.HORIZONTAL)
        coverage_sep.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(12, 6))

        ttk.Label(f, text="覆盖模式:", foreground="#0066cc").grid(row=2, column=0, sticky=tk.W, pady=3)
        coverage_mode_frame = ttk.Frame(f)
        coverage_mode_frame.grid(row=2, column=1, sticky=tk.W, pady=3)
        self._coverage_mode_radios = []
        for val, label in [("free", "🌐 自由模式（覆盖率自由分布）"),
                           ("center", "📍 双端固定（覆盖率集中在中间，两头主播）")]:
            rb = ttk.Radiobutton(coverage_mode_frame, text=label,
                                 variable=self.coverage_mode_var, value=val)
            rb.pack(side=tk.LEFT, padx=(0, 12))
            self._coverage_mode_radios.append(rb)

        # 素材占比数字输入框 0-100%（v2.10.40 改 Spinbox，原来用 ttk.Scale 滑块）
        ttk.Label(f, text="素材占比:", foreground="#0066cc").grid(row=3, column=0, sticky=tk.W, pady=(8, 3))
        mat_pct_frame = ttk.Frame(f)
        mat_pct_frame.grid(row=3, column=1, sticky=tk.EW, pady=(8, 3))
        mat_pct_frame.columnconfigure(1, weight=1)
        # Spinbox: 数字输入框（步长 1，可直接键入或点箭头）
        # v2.10.40 改：from_=0, to=100, increment=1（用户能精确填 50 而不是拖到 49.7）
        self.mat_pct_spinbox = ttk.Spinbox(
            mat_pct_frame,
            from_=0,
            to=100,
            increment=1,
            width=8,
            textvariable=self.mat_pct_var,
            font=("", 10, "bold"),
            foreground="#0066cc",
        )
        self.mat_pct_spinbox.grid(row=0, column=1, sticky=tk.W, padx=(0, 8))
        ttk.Label(mat_pct_frame, text="%", foreground="#0066cc", font=("", 10, "bold")).grid(row=0, column=2, sticky=tk.W)
        ttk.Label(mat_pct_frame, text="0%=纯主播，100%=纯素材（直接输入数字或点箭头调整）", foreground="#888", font=("", 8)).grid(row=1, column=1, sticky=tk.W, columnspan=2)

        # v2.10.24 新增：覆盖率约束总开关（关掉 → mat_pct/coverage_mode 不传，LLM 自由发挥）
        ttk.Label(f, text="覆盖率约束:", foreground="#0066cc").grid(row=4, column=0, sticky=tk.W, pady=(8, 3))
        self.enable_coverage_chk = ttk.Checkbutton(
            f,
            text="☑ 启用（关闭则 mat_pct/coverage_mode 不传给 LLM，回 v2.10.22 自由模式）",
            variable=self.enable_coverage_var,
            command=self._on_enable_coverage_toggle,
        )
        self.enable_coverage_chk.grid(row=4, column=1, sticky=tk.W, pady=(8, 3))

        # v2.10.25 新增：第二层审核开关（默认勾选；覆盖率约束关闭时强制禁用）
        ttk.Label(f, text="二层审核:", foreground="#0066cc").grid(row=5, column=0, sticky=tk.W, pady=(8, 3))
        self.second_review_chk = ttk.Checkbutton(
            f,
            text="☑ 启用（第一层 LLM 决策后，第二层 LLM 审核修订，达成 mat_pct 目标）",
            variable=self.second_review_var,
            command=self._on_second_review_toggle,
        )
        self.second_review_chk.grid(row=5, column=1, sticky=tk.W, pady=(8, 3))

        # v2.10.34「单次执行上限」已移到 1️⃣ 输入区 row 1（用过处理 Combobox 后面），
        # 这里不再单独占一行。

    def _on_mat_pct_change(self, *args):
        """素材占比变化 → 实时更新（v2.10.40 改 Spinbox 后保留此函数做兼容）"""
        # Spinbox 自动显示输入的数字，不再需要 Label 更新

    def _on_enable_coverage_toggle(self, *args):
        """覆盖率开关切换 → 启用/禁用 mat_pct Spinbox + coverage_mode radios + 二层审核 checkbox"""
        enabled = self.enable_coverage_var.get()
        state = "normal" if enabled else "disabled"
        # v2.10.40: mat_pct_scale → mat_pct_spinbox
        if hasattr(self, "mat_pct_spinbox"):
            self.mat_pct_spinbox.config(state=state)
        for rb in getattr(self, "_coverage_mode_radios", []):
            rb.config(state=state)
        # v2.10.25: 覆盖率约束关闭时，二层审核也强制关闭（无法审核 mat_pct）
        if hasattr(self, "second_review_chk"):
            self.second_review_chk.config(state=state)
            if not enabled:
                self.second_review_var.set(False)

    def _on_second_review_toggle(self, *args):
        """二层审核开关切换（目前无需联动，仅触发 cmd）"""
        pass

    # v2.10.10 已删除：_on_preset_mode_change 方法
    # 由"覆盖模式 + 素材占比"取代

    # ================================================================
    # v2.10 已删除：v1.83 密度档联动的 6 个方法（_on_density_change / _on_density_custom_change /
    #           _refresh_density_desc / get_density_x_seconds / calc_n_for_density /
    #           get_effective_n_for_prompt）。由 v2.10c 算法的"覆盖模式 + 素材占比"完全替代。
    # ================================================================

    # ================================================================
    # 3️⃣ 执行配置（沿用给 Tab 5）
    # ================================================================
    def _build_section_exec(self):
        f = ttk.LabelFrame(self.body, text="3️⃣ 执行配置（沿用给 Tab 5）", padding=8)
        f.pack(fill=tk.X, pady=5)
        f.columnconfigure(1, weight=1)

        # v2.10 已删除：v1.22 插入策略（free/sandwich/two_anchor Combobox）由"覆盖模式"完全替代

        # v2.10.35 输出分辨率已搬到「1️⃣ 输入」用过处理/单次上限同一行（全局结果参数，不属于执行细节）

        # 抽取方式
        ttk.Label(f, text="抽取方式:").grid(row=0, column=0, sticky=tk.W, pady=3)
        ext_row = ttk.Frame(f)
        ext_row.grid(row=0, column=1, sticky=tk.W, pady=3)
        for k, v in EXTRACT_OPTIONS_DISPLAY:
            ttk.Radiobutton(ext_row, text=v, variable=self.extract_mode_var, value=k).pack(side=tk.LEFT, padx=(0, 10))

        # 插入素材无声
        ttk.Checkbutton(f, text="🔇 插入素材无声（不勾 = 保留素材原声与主视频混音）",
                        variable=self.mute_material_var).grid(row=1, column=1, sticky=tk.W, pady=3)

        # 画面布局
        ttk.Label(f, text="画面布局:").grid(row=2, column=0, sticky=tk.W, pady=3)
        layout_combo = ttk.Combobox(f, textvariable=self.layout_var,
                                    values=[v for _, v in LAYOUT_OPTIONS_DISPLAY], state="readonly")
        layout_combo.grid(row=2, column=1, sticky=tk.EW, pady=3)

        # PIP 大小
        ttk.Label(f, text="PIP 大小:").grid(row=3, column=0, sticky=tk.W, pady=3)
        pip_row = ttk.Frame(f)
        pip_row.grid(row=3, column=1, sticky=tk.EW, pady=3)
        self.pip_scale = ttk.Scale(pip_row, from_=10, to=80, variable=self.pip_size_var, orient=tk.HORIZONTAL)
        self.pip_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.pip_size_label = ttk.Label(pip_row, text=f"{self.pip_size_var.get()}%", width=5, anchor="e")
        self.pip_size_label.pack(side=tk.LEFT, padx=(8, 0))

        def _update_pip_label(*args):
            self.pip_size_label.config(text=f"{self.pip_size_var.get()}%")
        self.pip_size_var.trace_add("write", _update_pip_label)

        # PIP 边距
        ttk.Label(f, text="PIP 边距:").grid(row=4, column=0, sticky=tk.W, pady=3)
        margin_row = ttk.Frame(f)
        margin_row.grid(row=4, column=1, sticky=tk.W, pady=3)
        ttk.Spinbox(margin_row, from_=0, to=200, width=6, textvariable=self.pip_margin_var).pack(side=tk.LEFT)
        ttk.Label(margin_row, text=" 像素（仅画中画时生效）", foreground="#888").pack(side=tk.LEFT, padx=4)

        # 用过素材处理
        ttk.Label(f, text="用过素材处理:").grid(row=5, column=0, sticky=tk.W, pady=3)
        cleanup_combo = ttk.Combobox(f, textvariable=self.cleanup_mode_var,
                                     values=[v for _, v in CLEANUP_OPTIONS_DISPLAY], state="readonly")
        cleanup_combo.grid(row=5, column=1, sticky=tk.EW, pady=3)
        ttk.Label(f, text="↳ 包括主素材（视频文件夹处理完后移走/删除/不处理）",
                  foreground="#888", font=("", 8)).grid(row=5, column=1, sticky=tk.W, pady=(0, 3))

        # 自动审核
        ttk.Checkbutton(f, text="⚡ 自动审核（勾上 = LLM 分析完直接推 Tab 5，不显示审核面板）",
                        variable=self.auto_review_var).grid(row=6, column=1, sticky=tk.W, pady=3)

    # ================================================================
    # 4️⃣ 启动批量分析
    # ================================================================
    def _build_section_run(self):
        f = ttk.LabelFrame(self.body, text="4️⃣ 启动批量分析", padding=8)
        f.pack(fill=tk.X, pady=5)
        f.columnconfigure(1, weight=1)

        # 并发数（v1.16）
        ttk.Label(f, text="并发数:").grid(row=0, column=0, sticky=tk.W, pady=3)
        conc_row = ttk.Frame(f)
        conc_row.grid(row=0, column=1, sticky=tk.W, pady=3)
        self.max_concurrent_var = tk.IntVar(value=3)
        ttk.Spinbox(conc_row, from_=1, to=10, increment=1, width=4,
                    textvariable=self.max_concurrent_var).pack(side=tk.LEFT)
        ttk.Label(conc_row, text=" 个同时跑（默认 3，1=串行）", foreground="#888").pack(side=tk.LEFT, padx=4)

        btn_row = ttk.Frame(f)
        btn_row.grid(row=1, column=1, sticky=tk.W, pady=(8, 0))
        self.start_btn = ttk.Button(btn_row, text="🚀 开始批量 AI 分析", command=self._on_start_batch)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btn_row, text="⏹ 停止", command=self._on_stop_batch, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=8)

        # v1.21：AI 输出目录（自动混剪后输出到这里，与手动混剪分开）
        out_row = ttk.Frame(f)
        out_row.grid(row=2, column=1, sticky=tk.EW, pady=(8, 0))
        out_row.columnconfigure(1, weight=1)
        ttk.Label(out_row, text="📤 AI 输出目录：").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(out_row, textvariable=self.ai_output_dir).grid(row=0, column=1, sticky=tk.EW, padx=4)
        ttk.Button(out_row, text="📁", width=4,
                   command=self._choose_ai_output_dir).grid(row=0, column=2)
        ttk.Label(out_row, text="（输出文件名带 _AI 后缀，自动持久化）",
                  foreground="#888").grid(row=1, column=1, sticky=tk.W, pady=(2, 0))

    def _on_start_batch(self):
        if self.is_running or self.active_workers > 0:
            messagebox.showinfo("提示", "已有任务在跑（或刚停止，worker 还在收尾），请等几秒再开始")
            return
        # 校验输入
        if not self.video_folder_var.get().strip():
            messagebox.showwarning("提示", "请先选视频文件夹")
            return

        # v1.44 跨 Tab 互斥：如果别的 Tab 在跑，弹窗询问排队
        if self.app is not None and hasattr(self.app, "task_queue") and self.app.task_queue.is_busy():
            current = self.app.task_queue.current_label()
            qsize = self.app.task_queue.queue_size()
            if not messagebox.askyesno(
                "任务互斥",
                f"⚠️ {current} 正在跑\n\n"
                f"📋 当前排队：{qsize} 个任务\n\n"
                f"选「是」加入排队\n选「否」取消本次运行"
            ):
                return
            display = f"Tab 6 AI 批量分析（{len(self.video_list) if hasattr(self, 'video_list') else '?'} 个视频）"
            self._log(f"📋 已加入排队（位置 {qsize + 1}）：{display}")
            self.app.task_queue.request(
                "Tab 6", display,
                callback=self._do_start_batch,
                on_done=None,
            )
            return
        if not self.material_folders:
            messagebox.showwarning("提示", "请至少添加一个素材文件夹")
            return
        # v2.10.54：每次点开始都重扫主文件夹（支持"用过的视频已移到子目录"后再次入队新一批）
        # 原逻辑只在 video_list 为空时刷新，第二次点开始时列表不空跳过，导致入队旧数据
        self._refresh_video_list()
        if not self.video_list:
            messagebox.showwarning("提示", "视频文件夹里没找到视频文件")
            return
        # 清队列 + 一次性入队（状态全排队中）
        self.queue.clear()
        # v2.10.37：ASR+LLM 单次分析上限（用户原意是「tab6 只做 10 条」，截断在入队前，不再让分析白白跑）
        videos_to_run = list(self.video_list)  # 复制一份，别动 self.video_list（重扫还要用）
        try:
            limit = int(self.max_batch_per_run_var.get())
        except (ValueError, tk.TclError):
            limit = 0
        if limit < 0:
            limit = 0
        total_videos = len(videos_to_run)
        if limit > 0 and total_videos > limit:
            skipped = total_videos - limit
            videos_to_run = videos_to_run[:limit]
            self._log(f"📊 单次分析上限 {limit}：本次只入队前 {limit} 条，跳过 {skipped} 条（剩余下次再分析，顺序按文件名字典序）")
        else:
            self._log(f"📊 待分析：{total_videos} 条（单次上限 {limit}={('不限制' if limit == 0 else '够跑，不截断')}）")
        # v1.41：每批开始重置跨视频素材去重集合
        with self._extract_lock:
            self._used_material_paths = set()
        for video_path in videos_to_run:
            self.queue.append({
                "video_path": video_path,
                "status": "排队中",
                "pushed": False,
            })
        self.active_workers = 0

        max_c = max(1, min(10, int(self.max_concurrent_var.get() or 3)))
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        # v1.47：直接执行路径（无其他 tab 占用）也要标 busy，否则全局状态条还是显示空闲
        try:
            if self.app is not None and hasattr(self.app, "task_queue"):
                self.app.task_queue.set_busy(
                    f"Tab 6 AI 批量分析（{len(videos_to_run)} 个视频）"
                )
                self._dbg(f"_on_start_batch set_busy 调到了：videos={len(videos_to_run)}, max_c={max_c}")  # v1.47 debug
        except Exception as e:
            self._dbg(f"_on_start_batch set_busy 异常：{e}")
        self._refresh_queue_table()
        self._log(f"🚀 开始批量分析：{len(videos_to_run)} 个视频（并发 {max_c}）")

        # 启动 N 个 worker
        for _ in range(min(max_c, len(videos_to_run))):
            self._dispatch_next()

    def _do_start_batch(self):
        """v1.44：被 task_queue 调度的 callback"""
        self._on_start_batch()

    def _on_stop_batch(self):
        self.is_running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        # v1.90：立刻释放全局任务队列的状态条（之前漏调，底部一直显示「正在跑」）
        try:
            if self.app is not None and hasattr(self.app, "task_queue"):
                self.app.task_queue.set_idle()
        except Exception as e:
            self._dbg(f"_on_stop_batch set_idle 异常：{e}")
        self._log("⏹ 已停止（在跑的 worker 会跑完当前视频）")

    # ---- v1.21：AI 输出目录选择 + 自动持久化 ----
    def _choose_ai_output_dir(self):
        path = filedialog.askdirectory(
            title="选择 AI 输出目录",
            initialdir=self.ai_output_dir.get() or "/Users/zxz/Desktop/口播成品",
        )
        if path:
            self.ai_output_dir.set(path)

    def _trace_ai_output_dir(self):
        """StringVar 变化时自动持久化到 config.yaml"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["ai_output_dir"] = self.ai_output_dir.get()
            # 调主窗口的 _save_config 写盘
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存 AI 输出目录失败：{e}")
        self.ai_output_dir.trace_add("write", _on_change)

    def _trace_industry(self):
        """StringVar 变化时自动持久化到 config.yaml"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["industry"] = self.industry_var.get().strip() or "通用"
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存视频行业失败：{e}")
        self.industry_var.trace_add("write", _on_change)
        # 注意：init 时不能用 after() 主动触发 _on_change，
        # 否则 StringVar 的默认值（"通用"）会覆盖 config.yaml 里已有的非默认值（如"南宁房产"）
        # trace 只在用户实际输入时触发 → 用户第一次输入即持久化

    # v2.10 已删除：_trace_insert_strategy（v1.44 插入策略持久化）由覆盖模式持久化替代

    def _trace_mat_pct(self):
        """v2.10.26：素材占比滑块变化时持久化"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["mat_pct"] = float(self.mat_pct_var.get())
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存素材占比失败：{e}")
        self.mat_pct_var.trace_add("write", _on_change)

    def _trace_extract_mode(self):
        """v2.10.40：抽取方式变化时持久化"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["extract_mode"] = self.extract_mode_var.get()
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存抽取方式失败：{e}")
        self.extract_mode_var.trace_add("write", _on_change)

    def _trace_coverage_mode(self):
        """v2.10.26：覆盖模式 radio 切换时持久化"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["coverage_mode"] = self.coverage_mode_var.get().strip() or "free"
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存覆盖模式失败：{e}")
        self.coverage_mode_var.trace_add("write", _on_change)

    def _trace_enable_coverage(self):
        """v2.10.26：覆盖率约束总开关切换时持久化"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["enable_coverage"] = bool(self.enable_coverage_var.get())
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存覆盖率约束开关失败：{e}")
        self.enable_coverage_var.trace_add("write", _on_change)

    def _trace_second_review(self):
        """v2.10.26：第二层审核开关切换时持久化"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["second_review"] = bool(self.second_review_var.get())
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存二层审核开关失败：{e}")
        self.second_review_var.trace_add("write", _on_change)

    def _trace_max_batch_per_run(self):
        """v2.10.34：单次执行上限变化时持久化"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            defaults = self.app.config.setdefault("defaults", {})
            # Spinbox 偶发返回空字符串 → 兜底用 0
            try:
                v = int(self.max_batch_per_run_var.get())
            except (ValueError, tk.TclError):
                v = 0
            defaults["max_batch_per_run"] = v
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception as e:
                    self._log(f"⚠️ 保存单次执行上限失败：{e}")
        self.max_batch_per_run_var.trace_add("write", _on_change)

    def _trace_video_folder(self):
        """v1.47：主视频文件夹变化时持久化"""
        def _on_change(*_):
            if self.app is None or not hasattr(self.app, "config"):
                return
            value = self.video_folder_var.get().strip()
            if not value:
                return
            defaults = self.app.config.setdefault("defaults", {})
            defaults["video_folder"] = value
            if hasattr(self.app, "_save_config"):
                try:
                    self.app._save_config()
                except Exception:
                    pass
        self.video_folder_var.trace_add("write", _on_change)
        # 注意：init 时不能用 after() 主动触发（理由同 _trace_industry）

    def _trace_material_folders(self):
        """v1.47：素材文件夹列表变化时持久化"""
        pass  # 改用 _save_material_folders() 在 add/delete/label 变化时主动调

    def _save_material_folders(self):
        """v1.47：把 material_folders 序列化写到 config.yaml"""
        if self.app is None or not hasattr(self.app, "config"):
            return
        try:
            folders_data = []
            for mf in self.material_folders:
                folders_data.append({
                    "path": mf.get("path", ""),
                    "label": mf.get("label", ""),
                    "enabled": bool(mf.get("enabled", True)),
                    "seconds": mf.get("seconds", 0),
                    "required": bool(mf.get("required", True)),
                })
            defaults = self.app.config.setdefault("defaults", {})
            defaults["material_folders"] = folders_data
            if hasattr(self.app, "_save_config"):
                self.app._save_config()
        except Exception as e:
            self._log(f"⚠️ 保存素材文件夹列表失败：{e}")

    def _dispatch_next(self):
        """找一个还在排队的 entry，启动 worker；找不到就检查是否全部完成"""
        if not self.is_running:
            return
        for idx, q in enumerate(self.queue):
            if q.get("status") == "排队中":
                q["status"] = "🚀 处理中"
                self.active_workers += 1
                self._refresh_queue_table()
                self._run_video_worker(idx, q)
                return
        # 没有 pending 了
        self._check_completion()

    def _check_completion(self):
        """active_workers==0 且没 pending → 全部完成
        v1.33：检查 queue 里是否还有"排队中"状态的视频，有就不算完成"""
        if self.active_workers > 0:
            return
        if not self.is_running:
            return
        # v1.33：还有排队中的视频就不算完成（避免误判提前结束）
        if any(q.get("status") == "排队中" for q in self.queue):
            # 还有 pending → 派下一个
            self._dispatch_next()
            return
        self.is_running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        # v1.44：释放全局任务队列
        try:
            if self.app is not None and hasattr(self.app, "task_queue"):
                self.app.task_queue.release()
        except Exception:
            pass
        self._log(f"✅ 批量分析完成（{len(self.queue)} 个视频）")
        try:
            messagebox.showinfo("完成", f"批量分析完成！共 {len(self.queue)} 个视频")
        except Exception:
            pass

    # ================================================================
    # 5️⃣ 队列 + 审核
    # ================================================================
    def _build_section_queue(self):
        f = ttk.LabelFrame(self.body, text="5️⃣ 处理队列 + 审核", padding=8)
        f.pack(fill=tk.BOTH, expand=True, pady=5)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)

        # 队列表格
        table_wrap = ttk.Frame(f)
        table_wrap.grid(row=0, column=0, sticky=tk.NSEW, pady=3)
        table_wrap.columnconfigure(0, weight=1)
        table_wrap.rowconfigure(0, weight=1)

        cols = ("idx", "name", "status", "info")
        self.queue_tree = ttk.Treeview(table_wrap, columns=cols, show="headings", height=8)
        self.queue_tree.heading("idx", text="#")
        self.queue_tree.heading("name", text="视频")
        self.queue_tree.heading("status", text="状态")
        self.queue_tree.heading("info", text="进度")
        self.queue_tree.column("idx", width=40, anchor="center")
        self.queue_tree.column("name", width=300, anchor="w")
        self.queue_tree.column("status", width=160, anchor="w")
        self.queue_tree.column("info", width=260, anchor="w")
        self.queue_tree.grid(row=0, column=0, sticky=tk.NSEW)
        qscroll = ttk.Scrollbar(table_wrap, orient=tk.VERTICAL, command=self.queue_tree.yview)
        qscroll.grid(row=0, column=1, sticky=tk.NS)
        self.queue_tree.configure(yscrollcommand=qscroll.set)

        self.queue_tree.bind("<Double-1>", self._on_queue_double_click)

        # 队列底部操作按钮
        qbtn_row = ttk.Frame(f)
        qbtn_row.grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        ttk.Button(qbtn_row, text="📝 审核选中", command=self._on_review_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(qbtn_row, text="🔄 重跑选中（ASR+LLM）", command=self._on_retry_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(qbtn_row, text="📤 推送选中到 Tab 5", command=self._on_push_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(qbtn_row, text="📤 推送全部到 Tab 5", command=self._on_push_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(qbtn_row, text="🚀 批量推 Tab 5 并执行", command=self._on_batch_push_and_run).pack(side=tk.LEFT, padx=2)

        # 二级审核面板（默认隐藏）
        self.review_frame = ttk.LabelFrame(self.body, text="📝 审核面板", padding=8)
        # 不 pack，等审核时再 pack

    def _refresh_queue_table(self):
        # 清空（保留展开的二级面板不重渲染）
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        for i, q in enumerate(self.queue):
            name = os.path.basename(q.get("video_path", ""))
            status = q.get("status", "等待")
            info_parts = []
            if "subtitles" in q:
                info_parts.append(f"字幕 {len(q['subtitles'])} 句")
            if "insertions" in q:
                info_parts.append(f"插入点 {len(q['insertions'])} 个")
            if "pushed" in q and q["pushed"]:
                info_parts.append("✅ 已推 Tab 5")
            info = " / ".join(info_parts)
            self.queue_tree.insert("", "end", iid=str(i), values=(i + 1, name, status, info))

    def _on_queue_double_click(self, event):
        self._on_review_selected()

    def _on_review_selected(self):
        sel = self.queue_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个视频")
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self.queue):
            return
        self._open_review_panel(idx)

    def _on_retry_selected(self):
        """v1.45：单独重跑选中行的 ASR + LLM（不影响 batch）"""
        sel = self.queue_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个视频")
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self.queue):
            return
        entry = self.queue[idx]
        video_name = os.path.basename(entry.get("video_path", ""))

        # 防止跟正在跑的冲突：如果这一行已经是分析中，不让重跑
        cur_status = entry.get("status", "")
        if cur_status.startswith("🎙") or cur_status.startswith("🤖"):
            messagebox.showwarning("提示", f"「{video_name}」正在分析中，请等它跑完再重试")
            return

        # 二次确认（避免误点浪费 ASR/LLM 配额）
        if not messagebox.askyesno(
            "重跑确认",
            f"重新分析「{video_name}」？\n\n"
            f"会清掉旧的字幕 / 插入点 / 素材结果，重新跑 ASR + LLM。\n"
            f"（不影响其他视频，不影响 batch 主状态）",
        ):
            return

        # 重置字段
        entry["subtitles"] = []
        entry["insertions"] = []
        entry["materials"] = []
        entry["pushed"] = False
        self._update_status(idx, "🔄 重跑中")

        # 启动独立 worker（不动 active_workers / is_running / _dispatch_next）
        self._run_retry_worker(idx, entry)

    def _run_retry_worker(self, q_idx: int, entry: dict):
        """v1.45：单条重跑 worker（不动 batch 状态）"""
        video_path = entry["video_path"]

        def worker():
            try:
                # Step 1: ASR
                self._update_status(q_idx, "🎙 ASR 字幕")
                subtitles = self._asr_real(video_path)
                entry["subtitles"] = subtitles
                if subtitles:
                    self._log(f"  🔄 [重跑] {os.path.basename(video_path)} 拿到 {len(subtitles)} 句字幕")
                else:
                    self._log(f"  ⚠️ [重跑] {os.path.basename(video_path)} ASR 没拿到字幕")

                # Step 2: LLM
                if subtitles:
                    self._update_status(q_idx, "🤖 LLM 分析")
                    insertions = self._llm_real(subtitles)
                else:
                    insertions = []
                entry["insertions"] = insertions

                # Step 3: 抽素材
                materials = self._extract_materials_for_insertions(insertions)
                entry["materials"] = materials

                self._update_status(q_idx, "✅ 完成（重跑）")
                self._log(f"  ✅ [重跑] {os.path.basename(video_path)} 完成")
            except Exception as e:
                entry["status"] = f"❌ 重跑错误: {e}"
                self._log(f"❌ [重跑] {os.path.basename(video_path)} 失败: {e}")
                self.root.after(0, self._refresh_queue_table)

        threading.Thread(target=worker, daemon=True).start()

    def _on_push_selected(self):
        sel = self.queue_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个视频")
            return
        idx = int(sel[0])
        if 0 <= idx < len(self.queue):
            self._push_one_to_tab5(self.queue[idx])

    def _on_push_all(self):
        pushed = 0
        for q in self.queue:
            if q.get("insertions") and not q.get("pushed"):
                self._push_one_to_tab5(q)
                pushed += 1
        self._log(f"📤 批量推送完成，共 {pushed} 个视频")
        messagebox.showinfo("完成", f"已推送 {pushed} 个视频到 Tab 5")

    # ================================================================
    # v1.21：批量推 Tab 5 并自动执行（流水线模式）
    # ================================================================
    def _on_batch_push_and_run(self):
        """
        把所有有 insertions 的视频推到 Tab 5，每个自动跑完再推下一个。
        """
        # 收集有 insertions 的项
        jobs = [q for q in self.queue if q.get("insertions") and q.get("materials")]
        if not jobs:
            messagebox.showwarning(
                "提示", "没有已完成 AI 分析的视频可执行\n（需要 queue 里状态为「完成」且有插入点）"
            )
            return

        # v2.10.34：单次执行上限（Spinbox 偶发返回空 → 兜底 0=不限制）
        try:
            limit = int(self.max_batch_per_run_var.get())
        except (ValueError, tk.TclError):
            limit = 0
        if limit < 0:
            limit = 0
        total_ready = len(jobs)
        if limit > 0 and total_ready > limit:
            skipped = total_ready - limit
            jobs = jobs[:limit]
            self._log(f"📊 单次执行上限 {limit}：本次只跑前 {limit} 条，跳过 {skipped} 条（顺序按 queue 列表）")
        else:
            self._log(f"📊 待执行：{total_ready} 条（单次上限 {limit}={('不限制' if limit == 0 else '够跑，不截断')}）")

        if not messagebox.askyesno(
            "确认",
            f"将把 {len(jobs)} 个视频依次推到 🎬 混剪/插段 执行\n"
            f"（每个视频会自动跑完混剪再下一个，期间请勿手动操作 Tab 5）\n\n开始？"
        ):
            return

        # v2.07：批量推送前预估整批素材是否够用
        warnings = self._check_supply(jobs)
        if warnings:
            head = "\n".join(warnings[:8])
            more = f"\n...还有 {len(warnings)-8} 条" if len(warnings) > 8 else ""
            if not messagebox.askyesno(
                "⚠️ 批量预估素材可能不够",
                f"{len(jobs)} 个视频总需求与现有量对比如下：\n\n{head}{more}\n\n是否仍继续批量推送？",
            ):
                self._log(f"⏸️ 用户取消批量推送（素材不足）")
                return

        # 构造 jobs
        self._ai_batch_jobs = []
        for q in jobs:
            self._ai_batch_jobs.append({
                "video_path": q["video_path"],
                "insertions": q["insertions"],
                "materials": q["materials"],
                "main_video_dir": self.video_folder_var.get().strip() if hasattr(self, "video_folder_var") else "",  # v1.49：原始主视频文件夹（用于主视频归档）
                "exec_config": {
                    "extract_mode": self.extract_mode_var.get(),
                    "mute_material": self.mute_material_var.get(),
                    "layout": self.layout_var.get(),
                    "pip_size": self.pip_size_var.get(),
                    "pip_margin": self.pip_margin_var.get(),
                    "cleanup_mode": self.cleanup_mode_var.get(),
                    "main_video_cleanup": self.main_video_cleanup_var.get(),  # v1.47：补传主视频处理方式
                },
                # v1.21：AI 输出目录 + 后缀
                "ai_output_dir": self.ai_output_dir.get().strip() or "/Users/zxz/Desktop/口播成品",
                "output_suffix": "_AI",
                # v2.10.14 新增：输出分辨率（推到 Tab 5, 默认 1080P）
                "output_resolution": self.output_resolution_var.get(),
            })
        self._ai_batch_index = 0
        self._ai_batch_results = []  # [{video, success}]

        self._log(f"🚀 启动 AI 批量执行：共 {len(self._ai_batch_jobs)} 个视频")
        # v1.46：标记全局状态条为忙碌（让其他 Tab 看到正在跑 + 防止重复点）
        if self.app is not None and hasattr(self.app, "task_queue"):
            self.app.task_queue.set_busy(f"AI 批量推 Tab 5（{len(self._ai_batch_jobs)} 个视频）")
            self._dbg(f"批量推 set_busy 调到了：jobs={len(self._ai_batch_jobs)}")  # v1.47 debug
            self._dbg(f"  exec_config={self._ai_batch_jobs[0]['exec_config'] if self._ai_batch_jobs else {}!r}")  # v1.47 debug
        # 切到 Tab 5 让用户看到流水线进度
        try:
            notebook = getattr(self.app, "notebook", None)
            if notebook is not None:
                for tab_id in notebook.tabs():
                    try:
                        if "混剪/插段" in notebook.tab(tab_id, "text"):
                            notebook.select(tab_id)
                            break
                    except Exception:
                        continue
        except Exception:
            pass
        self._ai_batch_run_next()

    def _ai_batch_run_next(self):
        """跑下一个 job（在主线程）"""
        if not hasattr(self, "_ai_batch_jobs"):
            return

        # v1.32：自动审核时可能多个 entry 同时入队，这里加 running 标志防重复启动
        if getattr(self, "_ai_batch_running", False):
            return

        if self._ai_batch_index >= len(self._ai_batch_jobs):
            # 全部完成
            total = len(self._ai_batch_results)
            ok = sum(1 for r in self._ai_batch_results if r["success"])
            fail = total - ok
            failed_items = [r for r in self._ai_batch_results if not r["success"]]

            self._log(f"✅ AI 批量执行完成：{total} 个视频（成功 {ok} / 失败 {fail}）")

            # v1.46：释放全局任务队列状态条
            if self.app is not None and hasattr(self.app, "task_queue"):
                try:
                    self.app.task_queue.set_idle()
                    self._dbg(f"批量推 set_idle 调到了（{ok}/{total} 成功）")  # v1.47 debug
                except Exception:
                    pass

            # v1.23：导出失败清单
            list_path = self._export_failed_list(failed_items)
            if list_path:
                self._log(f"📄 失败清单已导出：{list_path}")

            try:
                # 切回 Tab 6
                notebook = getattr(self.app, "notebook", None)
                if notebook is not None:
                    for tab_id in notebook.tabs():
                        try:
                            if "AI" in notebook.tab(tab_id, "text") or "ai" in notebook.tab(tab_id, "text").lower():
                                notebook.select(tab_id)
                                break
                        except Exception:
                            continue
            except Exception:
                pass

            # v1.23：批量执行报告弹窗（含失败清单 + 集中处理入口）
            self._show_batch_report(ok, fail, failed_items, list_path)
            # v1.32：清 running 标志，下次自动审核可重新触发
            self._ai_batch_running = False
            return

        # v1.33：启动新 job 前设 running=True（防多个 worker 完成时重复启动）
        self._ai_batch_running = True
        job = self._ai_batch_jobs[self._ai_batch_index]
        merge_tab = getattr(self.app, "merge_tab", None)
        if merge_tab is None or not hasattr(merge_tab, "load_from_ai_tab"):
            self._log("❌ Tab 5 没有 load_from_ai_tab 方法")
            self._ai_batch_running = False
            return

        self._log(
            f"📤 [{self._ai_batch_index + 1}/{len(self._ai_batch_jobs)}] "
            f"推 Tab 5：{os.path.basename(job['video_path'])}"
        )

        payload = {
            "video_path": job["video_path"],
            "insertions": job["insertions"],
            "materials": job["materials"],
            "main_video_dir": job.get("main_video_dir", ""),  # v1.49：原始主视频文件夹（用于主视频归档）
            "exec_config": job["exec_config"],
            # v1.21 自动执行标志
            "auto_run": True,
            "on_finished_callback": self._ai_batch_on_one_done,
            # v1.21 输出位置（避免临时目录被清导致视频丢失）
            "ai_output_dir": job.get("ai_output_dir", "/Users/zxz/Desktop/口播成品"),
            "output_suffix": job.get("output_suffix", "_AI"),
            # v2.10.35 修漏传：输出分辨率之前没传给 Tab 5，Tab 6 设的 720P/1080P 失效
            "output_resolution": job.get("output_resolution", "") or self.output_resolution_var.get(),
        }
        try:
            merge_tab.load_from_ai_tab(payload)
        except Exception as e:
            self._log(f"❌ 推送失败：{e}")
            # 标记失败，跑下一个
            self._ai_batch_results.append({
                "video": os.path.basename(job["video_path"]),
                "success": False,
            })
            self._ai_batch_index += 1
            self.root.after(0, self._ai_batch_run_next)

    def _ai_batch_on_one_done(self, result: dict):
        """
        Tab 5 跑完一个 job 后的回调（worker 线程）。
        v1.23：result = {"success": bool, "error": str, "stopped": bool}
        静默记录失败，不弹窗，全部跑完后统一汇总。
        """
        if not hasattr(self, "_ai_batch_jobs"):
            return
        if self._ai_batch_index >= len(self._ai_batch_jobs):
            return

        job = self._ai_batch_jobs[self._ai_batch_index]
        video_path = job["video_path"]
        video_name = os.path.basename(video_path)
        success = bool(result.get("success"))
        error_msg = (result.get("error") or "").strip()
        stopped = bool(result.get("stopped"))

        result_record = {
            "video": video_name,
            "video_path": video_path,
            "success": success,
            "error": error_msg,
            "stopped": stopped,
        }
        self._ai_batch_results.append(result_record)

        # 同步更新 queue 里对应视频的状态（让用户看到 ❌ 失败）
        for q in self.queue:
            if q.get("video_path") == video_path:
                if success:
                    q["status"] = "done"
                else:
                    q["status"] = "failed"
                    q["error_msg"] = error_msg
                break

        self._ai_batch_index += 1
        idx_done = self._ai_batch_index

        def _continue():
            status = "成功" if success else ("停止" if stopped else "失败")
            self._log(
                f"{'✅' if success else '❌'} [{idx_done}/{len(self._ai_batch_jobs)}] "
                f"{video_name}（{status}）" + (f" — {error_msg[:80]}" if not success else "")
            )
            # 刷新队列表（状态变红 + 重试按钮）
            try:
                self._refresh_queue_table()
            except Exception:
                pass
            # v1.32：清 running 标志，让 _ai_batch_run_next 能启动下一个 job
            self._ai_batch_running = False
            self._ai_batch_run_next()
        try:
            self.root.after(0, _continue)
        except Exception:
            _continue()

    # ================================================================
    # v1.23：失败清单导出 + 批量执行报告弹窗 + 集中处理面板
    # ================================================================
    def _export_failed_list(self, failed_items: list) -> str:
        """把失败清单写到 AI 输出目录，返回文件路径"""
        if not failed_items:
            return ""
        try:
            from datetime import datetime
            out_dir = self.ai_output_dir.get().strip() or "/Users/zxz/Desktop/口播成品"
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            list_path = os.path.join(out_dir, f"failed_list_{ts}.txt")
            with open(list_path, "w", encoding="utf-8") as f:
                f.write(f"AI 批量执行失败清单  {ts}\n")
                f.write(f"共 {len(failed_items)} 个失败\n")
                f.write("=" * 60 + "\n\n")
                for i, r in enumerate(failed_items, 1):
                    f.write(f"[{i}] {r['video']}\n")
                    f.write(f"    路径：{r.get('video_path', '')}\n")
                    f.write(f"    原因：{r.get('error', '未知')}\n\n")
            return list_path
        except Exception as e:
            self._log(f"⚠️ 失败清单导出失败：{e}")
            return ""

    def _show_batch_report(self, ok: int, fail: int, failed_items: list, list_path: str):
        """v1.23：跑完弹窗（统计 + 失败清单 + 集中处理入口）"""
        win = tk.Toplevel(self.root)
        win.title("AI 批量执行报告")
        win.geometry("640x420")
        win.transient(self.root)

        # 顶部统计
        header = ttk.Frame(win, padding=12)
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text=f"✅ 成功：{ok}    ❌ 失败：{fail}    总计：{ok + fail}",
            font=("Arial", 13, "bold"),
        ).pack(anchor=tk.W)

        if fail == 0:
            ttk.Label(header, text="🎉 全部成功！", foreground="green").pack(anchor=tk.W, pady=(8, 0))
        else:
            ttk.Label(header, text="⚠️ 部分失败，可点「去集中处理」调整后再试",
                      foreground="red").pack(anchor=tk.W, pady=(8, 0))

        # v1.33：输出位置（让用户知道视频在哪个文件夹）
        output_dir = self.ai_output_dir.get().strip() or "/Users/zxz/Desktop/口播成品"
        out_row = ttk.Frame(header)
        out_row.pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(out_row, text="📂 输出位置：").pack(side=tk.LEFT)
        ttk.Label(out_row, text=output_dir, foreground="#0066cc").pack(side=tk.LEFT)
        ttk.Button(
            out_row, text="📂 打开", width=6,
            command=lambda: self._open_in_finder(output_dir),
        ).pack(side=tk.LEFT, padx=4)

        # 失败清单
        if failed_items:
            ttk.Label(win, text="失败清单：", padding=(12, 0)).pack(anchor=tk.W)
            txt_frame = ttk.Frame(win, padding=(12, 4))
            txt_frame.pack(fill=tk.BOTH, expand=True)
            txt = tk.Text(txt_frame, wrap=tk.WORD, font=("Menlo", 10))
            scroll = ttk.Scrollbar(txt_frame, orient=tk.VERTICAL, command=txt.yview)
            txt.config(yscrollcommand=scroll.set)
            txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)
            for i, r in enumerate(failed_items, 1):
                txt.insert(tk.END, f"[{i}] {r['video']}\n")
                txt.insert(tk.END, f"    {r.get('error', '未知')[:200]}\n\n")
            txt.config(state=tk.DISABLED)

        if list_path:
            ttk.Label(win, text=f"📄 完整清单：{list_path}",
                      foreground="#666", padding=(12, 4)).pack(anchor=tk.W)

        # 底部按钮
        btn_row = ttk.Frame(win, padding=12)
        btn_row.pack(fill=tk.X)
        if fail > 0:
            ttk.Button(
                btn_row, text="📝 去集中处理失败",
                command=lambda: (win.destroy(), self._open_failed_panel()),
            ).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="关闭", command=win.destroy).pack(side=tk.RIGHT)

    def _open_in_finder(self, path: str):
        """v1.33：在 Finder 里打开指定路径"""
        import os, subprocess
        try:
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
            subprocess.Popen(["open", path])
        except Exception as e:
            self._log(f"⚠️ 打开文件夹失败：{e}")

    def _open_failed_panel(self):
        """v1.23：失败集中处理面板（每行一个失败视频）"""
        failed = [(i, q) for i, q in enumerate(self.queue) if q.get("status") == "failed"]
        if not failed:
            messagebox.showinfo("提示", "没有失败的视频需要处理")
            return

        win = tk.Toplevel(self.root)
        win.title(f"📝 失败集中处理（{len(failed)} 个）")
        win.geometry("820x520")
        win.transient(self.root)

        # 顶部：一键重试全部
        top = ttk.Frame(win, padding=10)
        top.pack(fill=tk.X)
        ttk.Label(top, text=f"共 {len(failed)} 个失败，可以单独编辑重试，或一键全部重试",
                  font=("Arial", 11)).pack(side=tk.LEFT)
        ttk.Button(
            top, text="🔄 一键重试全部失败",
            command=lambda: (win.destroy(), self._retry_all_failed()),
        ).pack(side=tk.RIGHT)

        # 列表
        wrap = ttk.Frame(win, padding=(10, 0))
        wrap.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=1, highlightbackground="#ddd")
        scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for q_idx, q in failed:
            row = ttk.Frame(inner, padding=6)
            row.pack(fill=tk.X, pady=2)

            video_name = os.path.basename(q["video_path"])
            ttk.Label(row, text=video_name, width=28, foreground="red",
                      font=("Arial", 10, "bold")).grid(row=0, column=0, sticky=tk.W, padx=4)

            err = (q.get("error_msg") or "未知错误").splitlines()[0][:80]
            ttk.Label(row, text=f"❌ {err}", foreground="#c00",
                      wraplength=380).grid(row=0, column=1, sticky=tk.W, padx=4)

            ttk.Button(row, text="📝 编辑",
                       command=lambda idx=q_idx: (win.destroy(), self._open_review_panel(idx))
                       ).grid(row=0, column=2, padx=4)
            ttk.Button(row, text="🔄 重试这个",
                       command=lambda idx=q_idx: self._retry_single_failed(idx, win)
                       ).grid(row=0, column=3, padx=4)

        # 底部
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=8)

    def _retry_single_failed(self, q_idx: int, parent_win=None):
        """重试单个失败视频"""
        if not (0 <= q_idx < len(self.queue)):
            return
        q = self.queue[q_idx]
        if q.get("status") != "failed":
            messagebox.showinfo("提示", "该视频不是失败状态")
            return
        if not q.get("insertions"):
            messagebox.showwarning("提示", "该视频没有插入点，无法重试")
            return

        # 状态重置为 "重试中"
        q["status"] = "retrying"
        try:
            self._refresh_queue_table()
        except Exception:
            pass

        # 构造 job
        job = {
            "video_path": q["video_path"],
            "insertions": q["insertions"],
            "materials": q.get("materials") or [],
            "exec_config": {
                "extract_mode": self.extract_mode_var.get(),
                "mute_material": self.mute_material_var.get(),
                "layout": self.layout_var.get(),
                "pip_size": self.pip_size_var.get(),
                "pip_margin": self.pip_margin_var.get(),
                "cleanup_mode": self.cleanup_mode_var.get(),
                "main_video_cleanup": self.main_video_cleanup_var.get(),  # v1.40
            },
            "ai_output_dir": self.ai_output_dir.get().strip() or "/Users/zxz/Desktop/口播成品",
            "output_suffix": "_AI",
            # v2.10.14 新增：输出分辨率（推到 Tab 5, 默认 1080P）
            "output_resolution": self.output_resolution_var.get(),
        }

        # 用单条流水线模式（不用 _ai_batch_index，避免干扰）
        if not hasattr(self, "_ai_single_jobs"):
            self._ai_single_jobs = []
        self._ai_single_jobs.append(job)

        if parent_win is not None:
            try:
                parent_win.destroy()
            except Exception:
                pass

        self._log(f"🔄 重试：{os.path.basename(q['video_path'])}")
        self._run_single_retry()

    def _run_single_retry(self):
        if not hasattr(self, "_ai_single_jobs") or not self._ai_single_jobs:
            return
        # 用 batch 的 _ai_batch_* 状态机，但 job 列表用 _ai_single_jobs
        if getattr(self, "_ai_batch_running", False):
            return
        # v1.32：running 标志交给 _ai_batch_run_next 自己管理（避免重复启动）
        self._ai_batch_jobs = self._ai_single_jobs
        self._ai_batch_index = 0
        self._ai_batch_results = []
        self._ai_single_jobs = []
        self._ai_batch_run_next()

    def _retry_all_failed(self):
        """一键重试全部失败视频"""
        failed = [(i, q) for i, q in enumerate(self.queue) if q.get("status") == "failed"]
        if not failed:
            messagebox.showinfo("提示", "没有失败的视频")
            return
        if not messagebox.askyesno("确认", f"将重新跑 {len(failed)} 个失败视频，确认？"):
            return

        jobs = []
        for q_idx, q in failed:
            if not q.get("insertions"):
                continue
            jobs.append({
                "video_path": q["video_path"],
                "insertions": q["insertions"],
                "materials": q.get("materials") or [],
                "exec_config": {
                    "extract_mode": self.extract_mode_var.get(),
                    "mute_material": self.mute_material_var.get(),
                    "layout": self.layout_var.get(),
                    "pip_size": self.pip_size_var.get(),
                    "pip_margin": self.pip_margin_var.get(),
                    "cleanup_mode": self.cleanup_mode_var.get(),
                },
                "ai_output_dir": self.ai_output_dir.get().strip() or "/Users/zxz/Desktop/口播成品",
                "output_suffix": "_AI",
                # v2.10.14 新增：输出分辨率（推到 Tab 5, 默认 1080P）
                "output_resolution": self.output_resolution_var.get(),
            })

        if not jobs:
            messagebox.showwarning("提示", "失败视频都没有插入点，无法重试")
            return

        # 直接用 batch 流水线模式
        if getattr(self, "_ai_batch_running", False):
            messagebox.showwarning("提示", "流水线已在跑，请等当前结束")
            return
        self._ai_batch_jobs = jobs
        self._ai_batch_index = 0
        self._ai_batch_results = []
        # v1.32：running 标志交给 _ai_batch_run_next 自己管理
        self._log(f"🔄 批量重试 {len(jobs)} 个失败视频")
        self._ai_batch_run_next()

    # ================================================================
    # 二级审核面板
    # ================================================================
    def _open_review_panel(self, idx):
        q = self.queue[idx]
        if "insertions" not in q:
            messagebox.showinfo("提示", "该视频还没分析完，请稍候")
            return

        # 先清掉旧的
        for w in self.review_frame.winfo_children():
            w.destroy()
        self.review_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.review_frame.config(text=f"📝 审核：{os.path.basename(q['video_path'])}")
        self.current_review_video = q["video_path"]

        # 字幕展示
        if q.get("subtitles"):
            self._render_review_subtitles(self.review_frame, q)

        # 插入点编辑列表
        self._render_review_insertions(self.review_frame, q)

        # 保存/取消
        bottom = ttk.Frame(self.review_frame)
        bottom.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(bottom, text="💾 保存审核", command=lambda: self._save_review(idx)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bottom, text="❌ 取消", command=self._close_review_panel).pack(side=tk.LEFT, padx=2)

    def _render_review_subtitles(self, parent, q):
        sf = ttk.LabelFrame(parent, text="💬 字幕（带 🔖 标记 AI 选中）", padding=6)
        sf.pack(fill=tk.X, pady=4)
        wrap = ttk.Frame(sf)
        wrap.pack(fill=tk.BOTH, expand=True)
        text = tk.Text(wrap, height=6, font=("", 9), wrap=tk.WORD)
        scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # 找出 AI 选中的字幕 idx
        marked_idx = set()
        for ins in q.get("insertions", []):
            for i in range(ins.get("subtitle_start_idx", 0), ins.get("subtitle_end_idx", 0) + 1):
                marked_idx.add(i)

        for i, sub in enumerate(q["subtitles"]):
            tag = " 🔖" if i in marked_idx else ""
            line = f"[{sub['start']:.1f}s-{sub['end']:.1f}s] {sub['text']}{tag}\n"
            text.insert(tk.END, line)
        text.config(state=tk.DISABLED)

    # v2.10.41: 删了 _match_folder_label_fuzzy 函数（30 行）
    # 原因：v2.10.41 改用 folder_index 编号方案，LLM 只输出 1/2/3 数字 → 软件翻译成完整 label
    # 数字绝不会写错，所有模糊匹配/包含关系兜底都不再需要

    def _render_review_insertions(self, parent, q):
        sf = ttk.LabelFrame(parent, text="🎯 插入点列表（可改时间/位置/mode）", padding=6)
        sf.pack(fill=tk.BOTH, expand=True, pady=4)

        wrap = ttk.Frame(sf)
        wrap.pack(fill=tk.BOTH, expand=True)

        # v1.91: 用 grid 布局, X 方向也加滚动条, 删掉强制 inner 宽度=canvas 宽度的配置
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        canvas = tk.Canvas(wrap, height=180, highlightthickness=1, highlightbackground="#ddd")
        canvas.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=canvas.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")

        x_scroll = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=canvas.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")

        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        canvas.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        # v2.07：算每个 folder_label 的需求 vs 现有
        demand = self._estimate_demand(q)
        label_to_mf = {mf.get("label", ""): mf for mf in self.material_folders if mf.get("enabled")}
        label_to_have = {label: self._count_remaining_videos(mf.get("path", ""))
                         for label, mf in label_to_mf.items()}

        # v2.10.36：Combobox 可用 label 列表（LLM 识别错的，用户能手动切换）
        all_labels = [mf.get("label", "") for mf in self.material_folders if mf.get("label")] or [""]
        # 给 label → 完整路径的 tooltip 映射
        label_to_path = {mf.get("label", ""): mf.get("path", "") for mf in self.material_folders}

        # 表头
        header = ttk.Frame(inner)
        header.pack(fill=tk.X, pady=(0, 2))
        for col, w in [("#", 3), ("起(秒)", 7), ("止(秒)", 7), ("位置", 18),
                        ("来源(文件夹)", 22), ("现有/预估", 11),  # v2.07 新增
                        ("原因", 30), ("", 3)]:
            ttk.Label(header, text=col, width=w).pack(side=tk.LEFT, padx=2)

        # 每行一个插入点
        for ins_idx, ins in enumerate(q["insertions"]):
            row = ttk.Frame(inner)
            row.pack(fill=tk.X, pady=1)

            ttk.Label(row, text=f"#{ins_idx+1}", width=3).pack(side=tk.LEFT, padx=2)

            start_v = tk.StringVar(value=f"{ins['start']:.1f}")
            ttk.Entry(row, textvariable=start_v, width=7).pack(side=tk.LEFT, padx=2)
            start_v.trace_add("write", lambda *a, i=ins_idx, v=start_v: self._safe_set_float(v, q["insertions"][i], "start"))

            end_v = tk.StringVar(value=f"{ins['end']:.1f}")
            ttk.Entry(row, textvariable=end_v, width=7).pack(side=tk.LEFT, padx=2)
            end_v.trace_add("write", lambda *a, i=ins_idx, v=end_v: self._safe_set_float(v, q["insertions"][i], "end"))

            # v1.22：position 默认显示中文（之前是英文 key，下拉框匹配不到会显示空白）
            pos_key = ins.get("position", "replaced")
            pos_disp = LAYOUT_DISPLAY.get(pos_key, "替换主材画面（铺满）")
            pos_v = tk.StringVar(value=pos_disp)
            pos_combo = ttk.Combobox(row, textvariable=pos_v, width=18, state="readonly",
                                     values=[v for _, v in LAYOUT_OPTIONS_DISPLAY])
            pos_combo.pack(side=tk.LEFT, padx=2)
            pos_v.trace_add("write", lambda *a, i=ins_idx, v=pos_v: self._set_position(i, v))

            # 来源（folder_label）— v2.10.41 简化：不再模糊匹配
            # LLM 输出 folder_index（1/2/3）→ _llm_real 已翻译成完整 folder_label
            # 必在 all_labels 里（_llm_real 兜底保证），不在就 fallback 到第 1 个
            current_label = ins.get("folder_label", "") or ""
            if current_label not in all_labels:
                self._log(f"  ⚠️ folder_label 异常: {current_label!r}，回退到第 1 个")
                current_label = all_labels[0] if all_labels else ""
                q["insertions"][ins_idx]["folder_label"] = current_label
            src_v = tk.StringVar(value=current_label)
            src_combo = ttk.Combobox(row, textvariable=src_v, width=22,
                                     values=all_labels, state="normal")
            src_combo.pack(side=tk.LEFT, padx=2)
            # v2.10.36 tooltip 鼠标悬停看完整路径
            tooltip_text = f"📁 {label_to_path.get(current_label, '(无路径)')}"
            self._create_tooltip(src_combo, lambda t=tooltip_text: t.replace("(无路径)", "未配置"))
            # 切换时更新 ins["folder_label"] + 刷新 tooltip
            def _on_label_change(*_, i=ins_idx, sv=src_v):
                q["insertions"][i]["folder_label"] = sv.get()
            src_v.trace_add("write", _on_label_change)

            # v2.07 现有/预估 显示列
            folder_label = ins.get("folder_label", "")
            have = label_to_have.get(folder_label, 0)
            need = demand.get(folder_label, 0)
            warn_text = f"⚠️ {have}/{need}" if (need > 0 and need > have) else f"{have}/{need}"
            warn_color = "#cc0000" if (need > 0 and need > have) else "#444"
            stock_lbl = ttk.Label(row, text=warn_text, width=11, foreground=warn_color, anchor="center")
            stock_lbl.pack(side=tk.LEFT, padx=2)

            # 原因
            reason_v = tk.StringVar(value=ins.get("reason", ""))
            ttk.Entry(row, textvariable=reason_v, width=30).pack(side=tk.LEFT, padx=2)
            reason_v.trace_add("write", lambda *a, i=ins_idx, v=reason_v: q["insertions"][i].update({"reason": v.get()}))

            # 删除
            ttk.Button(row, text="🗑", width=3,
                       command=lambda i=ins_idx: self._on_delete_insertion(q, i)).pack(side=tk.LEFT, padx=2)

        # 手动新增
        ttk.Button(inner, text="+ 手动新增插入点", command=lambda: self._on_add_insertion_manually(q)).pack(pady=4)

    def _set_position(self, idx, display_var):
        # 反查 display → key
        for k, v in LAYOUT_OPTIONS_DISPLAY:
            if v == display_var.get():
                self.queue_for_review() if hasattr(self, 'queue_for_review') else None
                # 找到当前打开的审核 queue
                for q in self.queue:
                    if idx < len(q.get("insertions", [])) and q["insertions"][idx].get("position") != k:
                        q["insertions"][idx]["position"] = k
                        return

    def _count_remaining_videos(self, folder_path: str) -> int:
        """v2.07：数素材文件夹里**还能用的**视频数（顶层 mp4/mov/mkv/avi/m4v/webm）。
        跳过 macOS AppleDouble `._` 隐藏文件；不算任何子目录（包括 used/）。
        """
        if not folder_path:
            return 0
        try:
            p = Path(folder_path)
            if not p.exists() or not p.is_dir():
                return 0
            count = 0
            for f in p.iterdir():
                if f.name.startswith("._"):
                    continue
                if f.is_file() and f.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}:
                    count += 1
            return count
        except Exception:
            return 0

    def _estimate_demand(self, q) -> dict:
        """v2.07：从 q['insertions'] 数每个 folder_label 出现几次（每次插入用 1 个素材）。
        返回 {folder_label: required_count}
        """
        demand = {}
        for ins in q.get("insertions", []):
            label = ins.get("folder_label", "")
            if not label:
                continue
            demand[label] = demand.get(label, 0) + 1
        return demand

    def _check_supply(self, jobs: list):
        """v2.07：检查所有 jobs 是否每个 folder 需求 ≤ 现有数量。
        jobs: list of q（队列项）。返回 warnings: list[str]
        """
        # 聚合需求
        total_demand = {}
        for q in jobs:
            for label, need in self._estimate_demand(q).items():
                total_demand[label] = total_demand.get(label, 0) + need
        if not total_demand:
            return []
        # 匹配 folder_label → material_folders
        label_to_mf = {mf.get("label", ""): mf for mf in self.material_folders if mf.get("enabled")}
        warnings = []
        for label, need in total_demand.items():
            mf = label_to_mf.get(label)
            if not mf:
                warnings.append(f"⚠️ 「{label}」在素材文件夹列表里找不到（可能被禁用/删除）")
                continue
            have = self._count_remaining_videos(mf.get("path", ""))
            if have == 0:
                warnings.append(f"⚠️ 「{label}」文件夹里 0 个视频（路径：{mf.get('path', '')}）")
            elif need > have:
                warnings.append(f"⚠️ 「{label}」需要 {need} 个，实际 {have} 个（缺 {need - have}）")
        return warnings

    def _safe_set_float(self, var, target, key):
        try:
            target[key] = float(var.get())
        except (tk.TclError, ValueError):
            pass

    def _on_delete_insertion(self, q, idx):
        if 0 <= idx < len(q["insertions"]):
            q["insertions"].pop(idx)
            self._refresh_queue_table()
            self._open_review_panel(self.queue.index(q))

    def _on_add_insertion_manually(self, q):
        q["insertions"].append({
            "start": 0.0, "end": 3.0, "position": "replaced",
            "folder_label": self.material_folders[0]["label"] if self.material_folders else "",
            "mode": "small", "reason": "手动新增",
            "subtitle_start_idx": 0, "subtitle_end_idx": 0,
            "material_seconds": 5,
        })
        self._refresh_queue_table()
        self._open_review_panel(self.queue.index(q))

    def _save_review(self, idx):
        q = self.queue[idx]
        self._log(f"💾 已保存审核：{os.path.basename(q['video_path'])} ({len(q['insertions'])} 个插入点)")
        self._refresh_queue_table()
        self._close_review_panel()

    def _close_review_panel(self):
        self.review_frame.pack_forget()
        for w in self.review_frame.winfo_children():
            w.destroy()
        self.current_review_video = None

    # ================================================================
    # 处理流水线（v1.14 真实 ASR/LLM + v1.16 并发 worker pool）
    # ================================================================
    def _run_video_worker(self, q_idx: int, entry: dict):
        """单视频 worker 线程：跑完递减 active_workers，再派下一个"""
        video_path = entry["video_path"]
        def worker():
            try:
                # Step 1: ASR 字幕（v1.14 真实调硅基流动）
                self._update_status(q_idx, "🎙 ASR 字幕")
                subtitles = self._asr_real(video_path)
                entry["subtitles"] = subtitles
                if subtitles:
                    self._log(f"  📝 {os.path.basename(video_path)} 拿到 {len(subtitles)} 句字幕")
                else:
                    self._log(f"  ⚠️ {os.path.basename(video_path)} ASR 没拿到字幕")

                # Step 2: LLM 分析（v1.14 真实调 Qwen）
                if subtitles:
                    self._update_status(q_idx, "🤖 LLM 分析")
                    insertions = self._llm_real(subtitles)
                else:
                    insertions = []
                entry["insertions"] = insertions

                # 抽取素材（按 folder_label 随机）
                materials = self._extract_materials_for_insertions(insertions)
                entry["materials"] = materials

                # 完成
                self._update_status(q_idx, "✅ 完成")
                # v2.07：每次 AI 分析完成后立刻检查素材够不够（需求 > 现有即警告）
                warnings = self._check_supply([entry])
                entry["_supply_warnings"] = warnings
            except Exception as e:
                entry["status"] = f"❌ 错误: {e}"
                self._log(f"❌ {os.path.basename(video_path)} 处理失败: {e}")
            finally:
                # 收尾：刷新 UI + 递减并发计数 + 派下一个
                def _after():
                    self.active_workers -= 1
                    self._refresh_queue_table()
                    # v2.07：分析完成后弹警告（仅在主线程触发 messagebox，避免 worker 直接碰 UI）
                    sw = entry.get("_supply_warnings") or []
                    if sw:
                        head = "\n".join(sw[:6])
                        more = f"\n...还有 {len(sw)-6} 条" if len(sw) > 6 else ""
                        self._log(f"⚠️ [{os.path.basename(entry.get('video_path',''))}] 素材预估：\n{head}{more}")
                    # 自动审核？（推到主线程，避免 worker 里直接碰 UI）
                    # v1.32：自动审核 = 推 + 自动跑（走 _ai_batch_jobs 串行机制，
                    # 解决"只推不跑导致 Tab 5 没主视频"问题）
                    if self.auto_review_var.get() and entry.get("insertions"):
                        self._auto_review_enqueue(entry)
                    if self.is_running:
                        self._dispatch_next()
                    else:
                        self._check_completion()
                self.root.after(0, _after)

        threading.Thread(target=worker, daemon=True).start()

    def _update_status(self, q_idx, status):
        self.queue[q_idx]["status"] = status
        self.root.after(0, self._refresh_queue_table)

    # ---- ASR（v1.14 真实）----
    def _asr_real(self, video_path: str):
        """
        真实 ASR：从设置页读 url/key/model，ffmpeg 抽音频 → POST → 解析
        返回 [{start, end, text}, ...]
        """
        cfg = {}
        if self.app is not None and hasattr(self.app, "config"):
            cfg = self.app.config.get("asr", {}) or {}

        url = (cfg.get("url") or "").strip() or ASR_DEFAULT_URL
        key = (cfg.get("key") or "").strip()
        model = (cfg.get("model") or "").strip() or ASR_DEFAULT_MODEL
        if not key:
            raise RuntimeError("ASR Key 未配置，请到「设置」Tab 填入并保存")

        duration = ASRClient.probe_duration(video_path)
        self._log(f"  📐 {os.path.basename(video_path)} 时长 {duration:.1f}s，开始 ASR...")

        client = ASRClient(url=url, key=key, model=model)
        return client.transcribe_with_duration(video_path, duration)

    # ---- LLM（v1.14 真实）----
    def _llm_real(self, subtitles: list):
        """
        真实 LLM：从设置页读 url/key/model，按 v2.10 覆盖模式 + 素材占比构造 prompt，
        让模型根据字幕和素材决定插入点。
        返回 [{start, end, folder_label, mode, reason, ...}, ...]

        v2.10.10 变更：移除 preset_mode 4 选 1 的客户端兜底分支
        （preset 1 全 folder 补全 / preset 2 必插补全 / preset 4 严格 N 都已删除）
        """
        cfg = {}
        if self.app is not None and hasattr(self.app, "config"):
            cfg = self.app.config.get("llm", {}) or {}

        url = (cfg.get("url") or "").strip() or LLM_DEFAULT_URL
        key = (cfg.get("key") or "").strip()
        model = (cfg.get("model") or "").strip() or LLM_DEFAULT_MODEL
        if not key:
            raise RuntimeError("LLM Key 未配置，请到「设置」Tab 填入并保存")

        client = LLMClient(
            url=url, key=key, model=model,
            temperature=LLM_TEMPERATURE, max_tokens=LLM_MAX_TOKENS,
        )

        # 字幕 + 素材 → prompt
        sub_lines = [
            f"[{i}] {s['start']:.1f}s-{s['end']:.1f}s  {s['text']}"
            for i, s in enumerate(subtitles)
        ]
        sub_text = "\n".join(sub_lines) if sub_lines else "（无字幕）"

        enabled_mats = [m for m in self.material_folders if m.get("enabled")]
        # v2.10.10 素材显示从单行升级多行：每条 mp4 = X 秒碎片 + 必插/可选
        # 关键词让 LLM 自己从 label 推断（用户不必手动填）
        mat_lines = []
        for i, m in enumerate(enabled_mats, 1):
            flag = "必插" if m.get("required") else "可选"
            sec = m.get("seconds", 5)
            mat_lines.append(
                f"{i}. 「{m['label']}」\n"
                f"   - 每条 mp4 = {sec} 秒碎片\n"
                f"   - {flag}"
            )
        mat_text = "\n".join(mat_lines) if mat_lines else "（无素材）"

        # v2.10.10 已删除：preset_mode 4 选 1 + preset_block + mode_intro 整块
# v2.10 起由"覆盖模式 + 素材占比 + 关键词字面命中"统一管理

        # v1.83 修：duration 必须在这里先赋值（密度档 N 计算要用）
        duration = subtitles[-1]["end"] if subtitles else 0

        # v2.10 已删除：密度档/density_label/insert_strategy/strategy_block（v1.83 + v1.22）
        # 由"覆盖模式 + 素材占比"完全替代

        # v1.24：行业上下文（让 LLM 不跑偏）
        industry = (self.industry_var.get() or "通用").strip() or "通用"
        industry_context = (
            f"\nVideo industry: {industry}\n"
            f"Use this to inform your decisions: subtitle topics, keyword interpretation, "
            f"and which material is most relevant for each insertion point.\n"
        ) if industry != "通用" else ""

        # ──────────── v2.10 新增：读取新变量 + 算术预算 ────────────
        # v2.10.24: 覆盖率约束总开关，关掉则不传 mat_pct/coverage_mode 给 LLM
        enable_coverage = self.enable_coverage_var.get()
        if enable_coverage:
            mat_pct = self.mat_pct_var.get()
            coverage_mode = self.coverage_mode_var.get()  # "free" or "center"
            total_mat_seconds = duration * mat_pct / 100
            target_mat = total_mat_seconds
            target_min = total_mat_seconds - 0.5
            target_max = total_mat_seconds + 0.5
            coverage_mode_desc = (
                "自由分布" if coverage_mode == "free"
                else "双端固定（素材集中在中间 20-80% 区域；头 0-20% + 尾 80-100% 优先让主播出镜，**但字幕明确命中具体地点时仍允许配素材**）"
            )
            # prompt 里 3 段要插的内容
            coverage_section = f"视频总时长: {duration:.1f}s\n素材占比目标: {mat_pct:.0f}%  → 目标素材总时长: {target_mat:.1f}s (允许区间 {target_min:.1f}s ~ {target_max:.1f}s)\n覆盖模式: {coverage_mode} ({coverage_mode_desc})"
            hard6_line = f"**硬约束 6**: 🚫 素材总时长必须在 {target_min:.1f}s ~ {target_max:.1f}s 区间内（{mat_pct:.0f}% 覆盖率目标，绝对不能 ≥ 视频时长；主播必须出镜）"
            self_check_q8 = f"8. **素材总时长落在 {target_min:.1f}s ~ {target_max:.1f}s 区间吗？** → 超出则删/减段"
            self_check_total = "8"
            # v2.10.52: 案例 1 的所有数字（每段时长、总素材、错误配对）都跟 mat_pct 联动
            ex1_target = round(12.8 * mat_pct / 100, 2)
            ex1_target_min = round(ex1_target - 0.5, 2)
            ex1_target_max = round(ex1_target + 0.5, 2)
            # 案例 1 三段，时长按比例分配（最小 1.5s）
            ex1_seg = max(round(ex1_target / 3, 1), 1.5)
            ex1_seg1 = ex1_seg
            ex1_seg2 = ex1_seg
            ex1_seg3 = round(ex1_target - ex1_seg1 - ex1_seg2, 1)
            if ex1_seg3 < 1.5:
                ex1_seg1 = ex1_seg2 = round(ex1_target / 2, 1)
                ex1_seg3 = round(ex1_target - ex1_seg1 - ex1_seg2, 1)
            # 错误配对：举"超额 + 不足"两个反例
            ex1_over = round(ex1_target * 1.5, 1)
            ex1_under = round(ex1_target * 0.4, 1)
        else:
            mat_pct = 50  # v2.10.51 占位值（关闭覆盖率时让 f-string 不报错）
            coverage_mode = coverage_mode_desc = target_mat = target_min = target_max = ""
            coverage_section = f"视频总时长: {duration:.1f}s\n（覆盖率约束已关闭，mat_pct/coverage_mode 不传，LLM 自由发挥）"
            hard6_line = ""
            self_check_q8 = ""
            self_check_total = "7"
            ex1_target = round(12.8 * mat_pct / 100, 2)
            ex1_target_min = round(ex1_target - 0.5, 2)
            ex1_target_max = round(ex1_target + 0.5, 2)

        # 必插清单
        required_labels = [m["label"] for m in enabled_mats if m.get("required")]
        required_str = "、".join(required_labels) if required_labels else "（无必插）"

        # v2.10.23 改写: 把 mat_pct/coverage_mode 传给 prompt + 案例 1 改为 50% 覆盖率 + 加硬约束 6
        # 解决: v2.10.22 的"全覆盖"问题（LLM 不知道目标覆盖率，按案例 1 模仿 → 80% 全覆盖）
        # 实测: v2.10.22 的 0708010 素材 18.0s / 视频 16.4s = 110%（主播不出镜）
        prompt = f"""你是南宁房产赛道的画面调度师。任务是为主播口播视频决定在哪里插入画面素材。

═══════════════════════════════════════
📌 任务理解（核心思考方式）
═══════════════════════════════════════
南宁房产销售类口播视频。
- 主角 = 主播（出镜讲话）
- 产品 = 房子（用样板间展示）
- 画面节奏 = 主播 + 样板间（产品）+ 地段配套（信任锚点）
- 目标 = 让口播不单调，但不能为了"不单调"而全用同一个 folder

═══════════════════════════════════════
📌 思考方式（4 步走）
═══════════════════════════════════════
1. **先看字幕里的"地点/配套关键词"**（地铁口/凤林北/山姆/爱琴海/户型词）
2. **命中关键词 → 配对应 folder**（地铁口/万达/爱琴海/山姆/样板间）
3. **没命中关键词的"纯价格段"**（如"18万/便宜800"）→ 配"具体素材"段里标注为「必插」的 folder 兜底
4. **跳过"纯 CTA/感叹词"段**（"上车啊/叫吧/妈呀/老表/😊"）→ 让主播出镜

═══════════════════════════════════════
📌 必出 folder 清单（用户在素材列表勾选 ⭐ 必插）
═══════════════════════════════════════
**{required_str}**

⚠️ 重要：用户勾选的「必插」folder 必须整个视频至少出现 1 次（位置自由，开头/中间/结尾都行，最多 2 段，2 段之间必须夹其他 folder）。
如果用户没勾任何必插（清单是"（无必插）"），就完全按关键词字面命中 + 思考方式走，不要强行加任何 folder。

═══════════════════════════════════════
📦 folder 业务概念表（仅作匹配参考）
═══════════════════════════════════════
下方"具体素材"段列出了 folder 的**编号 + 实际完整 label 字符串**（用户自命名）。
**输出时只填 `folder_index`（数字 1/2/3...），不要写完整 label 字符串**，避免拼错！

| 业务概念 | 角色 | 触发关键词 |
| --- | --- | --- |
| 万达 | 商业中心 | 字幕提到 "凤林北/凤陵北/万达" |
| 地铁口 | 地段交通 | 字幕提到 "地铁口/一号线/廊东/琅东/双地铁" |
| 山姆 | 商业配套 | 字幕提到 "山姆/超市/买菜/商场" |
| 样板间 | 产品户型 | 字幕提到 "两房/70年/装修/开发商/128平/全新没住过/三房二厅/几房几厅" |

⚠️ **folder_index 填写规则**：
- ❌ 错：folder_index="地铁口"（业务名）
- ❌ 错：folder_index="附近配套山姆"（label 文字）
- ✅ 对：folder_index=1（数字 1，对应"具体素材"段第 1 条）
- ✅ 对：folder_index=2（数字 2，对应"具体素材"段第 2 条）

具体素材（编号 → label）:
{mat_text}

═══════════════════════════════════════
🎬 字幕
═══════════════════════════════════════
{sub_text}

{coverage_section}

═══════════════════════════════════════
📚 案例（看正反例，对照学习）
═══════════════════════════════════════

【案例 1: 纯价格 + 户型词 字幕（无明确地点）】

字幕示例（12.8s 视频，素材占比 {mat_pct:.0f}% → 目标素材 ≈ {ex1_target:.1f}s）: 6年回本/18万/两房25万/便宜800/70年全新没住过

✅ **正确配对**（演示配对逻辑，比例看你上方 {mat_pct:.0f}% 目标，总素材 ≈ {ex1_target:.1f}s = 12.8s × {mat_pct:.0f}%）:
- 段A（中间位置）{ex1_seg1}s 样板间 | 命中"两房"户型词，配样板间
- 段B（结尾位置）{ex1_seg2}s 样板间 | 命中"70年/全新没住过"户型词
- 段C（位置自由，开头/中间/结尾都行）{ex1_seg3}s 爱琴海 | 字幕"6年回本"无明确地点，配地段地标
说明: 上面 3 段总素材 ≈ {ex1_target:.1f}s（按 mat_pct={mat_pct:.0f}% 计算）。**你的真实目标是 [{ex1_target_min:.1f}s, {ex1_target_max:.1f}s]**，段数和单段时长按你的视频长度和目标计算，**不要照抄这里的 {ex1_seg1}/{ex1_seg2}/{ex1_seg3}** — 这些只是展示"每个有命中的关键词就该配 1 段"的逻辑

❌ **错误配对**（不要这样）:
- 4 段全爱琴海覆盖全视频，总素材 ≈ {ex1_over}s（违反硬2，户型被漏配，**且素材远超 {ex1_target:.1f}s 目标 {ex1_over - ex1_target:.1f}s**）
- 只配 1 段爱琴海总素材 ≈ {ex1_under}s（漏掉"两房/70年"户型，**且素材低于 {ex1_target:.1f}s 目标 {ex1_target - ex1_under:.1f}s**）
- 段与段时间区间重叠（如 [5.0-10.0] 和 [6.2-11.2] 几何重叠 6.2-10.0）

【案例 2: 混合字幕（部分命中关键词）】

字幕示例: 在凤林北/双地铁/三房二厅/25万/学区/赶紧看房

✅ **正确配对**:
- [0.0-3.0] 3.0s 万达 | 命中"凤林北"
- [3.0-6.0] 3.0s 地铁口 | 命中"双地铁"
- [6.0-8.0] 2.0s 样板间 | 命中"三房二厅"
- "学区/赶紧看房" 跳过（CTA 段）
- 如果用户勾了爱琴海为「必插」，补 1 段爱琴海（位置灵活 — 可开头/可中间/可结尾）；没勾就不补

❌ **错误配对**（不要这样）:
- 把"凤林北"配成爱琴海（应配万达）
- 把"赶紧看房"也配成素材（应跳过 CTA）
- 段与段时间区间重叠

【案例 3: 纯 CTA/感叹词字幕】

字幕示例: 这辈子还没买房/在广西的老表/你能刷到我/那是你家祖坟冒烟/看到没有/11万来一套/我的天哪/赶紧退租

✅ **正确配对**:
- 几乎全跳过 CTA 段
- 只在"11万来一套"等价格区配 1 段爱琴海 [5.0-10.0]

❌ **错误配对**（不要这样）:
- 每段 CTA 都配素材
- 把"老表/妈呀/天哪"也配成素材

═══════════════════════════════════════
⚠️ 案例数字 vs 你的目标（重要！）
═══════════════════════════════════════
上面 3 个案例的素材占比数字（50%、总素材 8.6s/6.4s 等）**只是演示"命中关键词 → 配对应 folder"的配对逻辑**，不是让你照抄的覆盖率数字。
**实际配段时按你上方看到的 {mat_pct:.0f}% 目标配**：
- 30-50% → 段数少、主播出镜时间长（案例 1 接近这种）
- 60-80% → 段数中等、主播出镜时间中等
- 80-100% → 段数多、主播出镜时间短（几乎全程素材，CTA 段也尽量配点）

═══════════════════════════════════════
🚨 5 条硬约束（违反即不合格）
═══════════════════════════════════════
**硬约束 1**: 字面命中优先（见上表 + 案例）
**硬约束 2**: 🚫 禁止连续 N>1 段同 folder + 禁止任意两段 [s1,e1][s2,e2] 区间重叠
            任何 folder 最多 2 段，2 段间必须夹 ≥ 1 个其他 folder
**硬约束 3**: 🚫 禁止短时长插入（5s 素材 ≥ 2.5s，3s 素材 ≥ 1.5s）
**硬约束 4**: 用户勾选的「必插」folder 必须每个出现 ≥ 1 次（**位置自由**：开头/中间/结尾都行，由字幕实际命中的关键词决定）。如果清单是"（无必插）"，不强制任何 folder。
**硬约束 4-bis（关键节点必须覆盖）**: 字幕里出现的每个 非 CTA 关键节点（地铁口/凤林北/万达/山姆/户型词）必须 ≥ 1 段对应 folder 覆盖，**关键节点漏配 = 输出不合格**。命中词对照：
       - "地铁口 / 一号线 / 200米 / 双地铁" → 至少 1 段 地铁口
       - "凤林北 / 万达 / 商业中心" → 至少 1 段 万达
       - "山姆 / 超市 / 买菜 / 配套" → 至少 1 段 山姆
       - "几房几厅 / 三房二厅 / 户型 / 装修 / 70年全新" → 至少 1 段 样板间
       - "学区 / 楼下就是 / 出门 / 商圈"（地段描述词） → 至少 1 段 任意地段 folder（爱琴海/万达/地铁口/山姆 任一）
       - 多个命中同 folder 可合并成 1 段，但**不能因为合并就把别的命中漏了**
**硬约束 5**: 🚫 禁止为 纯 CTA/感叹词/emoji/数字 段插入素材（**例外**：如果某段同时含关键节点 + CTA 词，**关键节点优先**，允许在该段覆盖；详见下方"冲突解决优先级"）

═══════════════════════════════════════
🛡 冲突解决优先级（多规则打架时按这个顺序取舍，前面优先）
═══════════════════════════════════════
当多条硬约束打架时，**按下面优先级从高到低取舍**：

🥇 **第 1 优先：关键节点全覆盖**（硬约束 4-bis）
- 字幕里每个非 CTA 关键节点必须有 ≥ 1 段对应 folder
- 哪怕这一段覆盖了 CTA 段、哪怕跟前一段同 folder、哪怕总素材超出覆盖率上限，**都不能漏**
- 这是核心价值，别的规则都为它让路

🥈 **第 2 优先：单条硬约束内部**（硬约束 2 / 5 / 7 平级）
- 同一规则内的小冲突（如连续同 folder → 合并；重叠 → 缩短）
- 但**跟第 1 优先冲突时，必须让路给关键节点**

🥉 **第 3 优先：覆盖率 + 单段时长**（硬约束 6 / 3）
- 总素材超目标区间时，**优先缩/删非关键节点段**（缩到 min_dur 后还超就删）
- 关键节点段**最后才动**

**具体场景示例**（按优先级判断）：
- 场景 A：必须为关键节点段配素材，但会盖住 CTA 段 → **关键节点优先，CTA 让路**（不是 CTA 让位，是这条 CTA 段被覆盖了）
- 场景 B：必须为关键节点段配素材，但跟前一段同 folder → **关键节点优先**，用第 1 个段夹中间一个不同 folder 解决（或合并）
- 场景 C：关键节点全配齐后总素材超覆盖率上限 → 缩/删 CTA 段或非命中段，**关键节点段不动**

{hard6_line}
**硬约束 7**（几何合并）: 🚫 任意两段 [s1,e1][s2,e2] 区间重叠时，必须在 LLM 输出前**主动合并**成一段（取并集），或缩短其中一段以消除重叠
            （不要等客户端后处理，LLM 自己就要合并）

═══════════════════════════════════════
📤 输出前 7 问自检（必须脑内走一遍）
═══════════════════════════════════════
1. **任意两段 [s1,e1] [s2,e2] 区间重叠吗？（含边界）** → 重叠的必须在输出前**主动合并**（取并集）或缩短其中一段，绝对不能输出 [11.7-16.7] 和 [13.3-18.3] 这种重叠
2. 有连续 2+ 段同 folder？→ 合并
3. 爱琴海段数 > 2？→ 删多余
4. 字幕里的 户型词 是否优先配样板间？→ 改配
5. 有没有为 纯 CTA/感叹词/数字 配了素材？（**如果是纯 CTA 段就删；如果该段同时含关键节点，按"冲突解决优先级"保留**）
6. 5s 素材只用了 < 2.5s？→ 缩短或删
7. **字幕里提到的每个关键节点（地铁口/万达/山姆/户型词/地段描述词）都有 ≥ 1 段对应 folder 覆盖吗？** → 把字幕内容拿出来逐个检查，**漏一个关键节点 = 输出不合格**，必须补上
{self_check_q8}

{self_check_total} 问都通过，再输出 JSON。

═══════════════════════════════════════
📤 输出
═══════════════════════════════════════
JSON:
{{
  "insertions": [
    {{"start": <float>, "end": <float>, "folder_index": <int 1/2/3...>, "reason": "<30字内>"}}
  ]
}}

只返回 JSON，不要其他文字。"""

        self._log(f"  🤖 调第一层 LLM：{model} @ {url}，{len(subtitles)} 句字幕 + {len(enabled_mats)} 个素材")
        insertions = client.chat_json(prompt)
        # v2.10.41 翻译：LLM 输出 folder_index（1/2/3）→ 软件翻译成 folder_label（完整字符串）
        # 编号绝不会写错，根治 v2.10.40 之前所有"label 拼错/简化"的问题
        translation_log = []
        for i, ins in enumerate(insertions):
            fi = ins.get("folder_index")
            if isinstance(fi, int) and 1 <= fi <= len(enabled_mats):
                ins["folder_label"] = enabled_mats[fi - 1]["label"]
                translation_log.append(f"  [{i}] folder_index={fi} → {ins['folder_label']}")
            else:
                # 越界/缺失：降级到第 1 个（兜底不致命）
                fallback_label = enabled_mats[0]["label"] if enabled_mats else ""
                ins["folder_index"] = 1
                ins["folder_label"] = fallback_label
                translation_log.append(f"  [{i}] folder_index={fi!r} 越界 → 兜底用 1 ({fallback_label})")
        if translation_log:
            self._log(f"  🔢 folder_index 翻译：\n" + "\n".join(translation_log))

        # v2.10.25 第二层审核：第一层 LLM 出方案 → 第二层 LLM 做减法（达成 mat_pct 目标）
        second_review_enabled = self.second_review_var.get() and self.enable_coverage_var.get() and bool(target_max)
        if second_review_enabled and insertions:
            # v2.10.27: 让 _apply_revisions 知道视频时长（center 模式 move 边界用）
            self._current_video_duration = duration
            self._log(f"  🔁 调第二层 LLM 审核（{len(insertions)} 段，goal [{target_min:.1f}s, {target_max:.1f}s]）")
            revisions = self._second_review(
                client, insertions, subtitles, duration,
                target_min=target_min, target_max=target_max,
                mat_pct=mat_pct, coverage_mode=coverage_mode,
            )
            insertions = self._apply_revisions(insertions, revisions)

        # v2.10 已删除：v1.83 客户端兜底校验（sandwich/two_anchor 软引导）
        # v2.10 起不再区分策略，由"覆盖模式 + 算术硬约束"统一处理

        # v1.37 客户端兜底：每个插入点的 end-start 必须 ≤ 素材秒数 AND end 必须 ≤ 主视频时长
        # （两层保护：素材秒数上限 + 主视频剩余时间上限）
        mat_secs = {m["label"]: float(m.get("seconds", 5)) for m in self.material_folders if m.get("enabled")}
        truncated = 0
        removed = 0
        new_insertions = []
        for ins in insertions:
            label = ins.get("folder_label", "")
            max_sec = mat_secs.get(label, 5.0)
            try:
                s = float(ins.get("start", 0))
                e = float(ins.get("end", 0))
            except (TypeError, ValueError):
                self._log(f"  🗑️ 字段非法 [{label}]：start/end 不是数字")
                removed += 1
                continue

            # v1.37 保护 1：start 已经在主视频外（直接剔除，没有可用空间）
            if s >= duration:
                self._log(f"  🗑️ 超出主视频 [{label}]：start={s:.1f}s ≥ 主视频时长 {duration:.1f}s")
                removed += 1
                continue
            if s < 0:
                s = 0.0
                ins["start"] = s

            # v1.39 保护 2：实际可用时长 = min(素材秒数, 主视频剩余时间)
            available = min(max_sec, duration - s)
            if available < 1.0:
                self._log(f"  🗑️ 无可用空间 [{label}]：start={s:.1f}s，剩余 {duration - s:.1f}s < 1.0s（小于 1s 体验差，剔除）")
                removed += 1
                continue

            cur_dur = e - s
            truncated_reason = None
            if cur_dur > max_sec:
                truncated_reason = f"素材上限 {max_sec:.1f}s"
            if e > duration:
                t2 = f"主视频时长 {duration:.1f}s"
                truncated_reason = (truncated_reason + " + " + t2) if truncated_reason else t2

            if truncated_reason or cur_dur <= 0:
                new_end = round(s + available, 2)
                if truncated_reason:
                    self._log(f"  ⚠️ 时长截断 [{label}]：{s:.1f}-{e:.1f} → {s:.1f}-{new_end:.1f} ({truncated_reason})")
                else:
                    self._log(f"  ⚠️ 时长非法 [{label}]：{s:.1f}-{e:.1f} (≤0)，强制设为 {s:.1f}-{new_end:.1f}")
                ins["end"] = new_end
                truncated += 1

            new_insertions.append(ins)

        insertions = new_insertions
        if truncated:
            self._log(f"  📏 共截断 {truncated} 个超时长插入点")
        if removed:
            self._log(f"  🗑️ 共剔除 {removed} 个超出主视频/无效插入点")

        # v2.10.10 已删除：preset_mode 4 选 1 的 3 个客户端兜底分支
        # （preset 1 全 folder 补全 / preset 2 必插补全 / preset 4 严格 N）
        # v2.10 起由 prompt 内的"覆盖模式 + 算术硬约束" + 素材清单硬要求统一处理
        # v1.85 已移除"必插文件夹最多 1 次"强制（让 LLM 自觉）

        # 字段兜底
        for ins in insertions:
            ins.setdefault("position", "replaced")
            ins.setdefault("mode", "small")
            ins.setdefault("reason", "")
            try:
                ins["start"] = float(ins["start"])
                ins["end"] = float(ins["end"])
            except (TypeError, ValueError, KeyError):
                raise RuntimeError(f"LLM 输出 start/end 不是数字：{ins}")

        # v2.10.42 重新启用算术兜底（v2.10.18 被禁，结果 LLM 经常超 target_max 失守覆盖率）
        required_labels = {m["label"] for m in self.material_folders if m.get("enabled") and m.get("required")}
        mat_pct_for_post = self.mat_pct_var.get() if self.enable_coverage_var.get() else None
        if mat_pct_for_post is not None:
            insertions = self._postprocess_insertions(
                insertions, mat_secs, duration, mat_pct_for_post,
                required_labels=required_labels,
            )
        return insertions

    def _build_second_review_prompt(self, insertions, subtitles, duration, target_min, target_max, mat_pct, coverage_mode, coverage_mode_desc):
        """v2.10.25 第二层 LLM 审核 prompt 模板（让 LLM 做减法，达成 mat_pct 目标）

        Returns:
            str: 完整的 prompt 文本
        """
        # 第一层 insertions 转成可读的列表
        ins_lines = []
        total = 0.0
        for i, ins in enumerate(insertions):
            try:
                s = float(ins.get("start", 0))
                e = float(ins.get("end", 0))
            except (TypeError, ValueError):
                continue
            d = round(e - s, 2)
            total += d
            label = ins.get("folder_label", "?")
            reason = (ins.get("reason", "") or "")[:30]
            ins_lines.append(f"[{i}] [{s:.1f}-{e:.1f}] {d:.2f}s {label} | {reason}")
        ins_text = "\n".join(ins_lines) if ins_lines else "（无）"
        total_str = f"{total:.1f}s ({total/duration*100:.0f}%)" if duration else f"{total:.1f}s"

        # 字幕片段（让 LLM 看到上下文以决定减谁）
        sub_lines = [f"[{i}] {s['start']:.1f}s-{s['end']:.1f}s  {s['text']}" for i, s in enumerate(subtitles)]
        sub_text = "\n".join(sub_lines)

        prompt = f"""你是南宁房产视频的覆盖审核师。
第一层 LLM 已插了 {len(insertions)} 段素材（共 {total_str}）。你来做审核修订，达成 {mat_pct}% 覆盖率目标。

═══════════════════════════════════════
📋 原始字幕（{len(subtitles)} 句）
═══════════════════════════════════════
{sub_text}

═══════════════════════════════════════
📌 第一层插入方案（共 {total_str}）
═══════════════════════════════════════
{ins_text}

═══════════════════════════════════════
🎯 修正目标
═══════════════════════════════════════
- 视频总时长: {duration:.1f}s
- 目标素材总时长: {target_min:.1f}s ~ {target_max:.1f}s（{mat_pct}% 覆盖率）
- 当前素材总时长: {total:.1f}s（{'超出 ' + format(total - target_max, '.1f') + 's' if total > target_max else '低于 ' + format(target_min - total, '.1f') + 's' if total < target_min else '在区间内'}）
- 覆盖模式: {coverage_mode} ({coverage_mode_desc})

═══════════════════════════════════════
🛠 审核任务（按优先级减段 / 缩段）
═══════════════════════════════════════
**优先级 A — 必保留**（用户决定，不许删）：
  - 用户勾选「必插」⭐ 的 folder 每段都必须 ≥ 1 次（清单是"（无必插）"时不强制任何 folder）
  - 如果上面是必插清单是"（无必插）"，没有"必保留"约束，**全部段都按 B/C 优先级处理**

**优先级 B — 优先保留**（除非总时长爆 target_max）：
  - 字面命中段（地铁口/凤岭北/万达/山姆/户型词）— 总时长超上限时**优先缩短这些段**到 ≤ 2.5s（保留前段命中关键词部分），不直接 discard

**优先级 C — 可删**：
  - 重复/相邻同 folder 的第二段（合并成 1 段）
  - 短时长 < 2.5s 或无命中关键词的段落
  - 起始点远离关键词命中区的段落

**调整动作**：
- `keep`: 保留原样
- `shorten`: 缩短到 new_end（保留命中关键词段，删尾部不命中部分）
- `discard`: 删除该段
- `move`: 移到 new_start（**仅 center 模式**；时长不变，把段挪到中心区 [20%-80%]）

═══════════════════════════════════════
🎯 center 模式专属审核（仅当覆盖模式 = center）
═══════════════════════════════════════
- 头 0-20% 区域（0 ~ {duration * 0.2:.1f}s）和 尾 80-100% 区域（{duration * 0.8:.1f}s ~ {duration:.1f}s）**优先让主播出镜**
- **但字幕明确命中具体地点词（如"下楼就是 / 出门 200 米 / 走路 5 分钟"）时该段允许保留在原位置**，不必 move
- 完全没有命中具体地点的段，落在头尾两个区域 → `move` 到中心区
- 中心区范围：[{duration * 0.2:.1f}s, {duration * 0.8:.1f}s]
- 如果多段都要 move 进来，按原始 start 顺序紧凑排列（不重叠），间隔 0.5-1.0s

═══════════════════════════════════════
🚨 约束
═══════════════════════════════════════
1. 输出后的总素材时长必须落在 {target_min:.1f}s ~ {target_max:.1f}s 区间内
2. 用户勾选的「必插」folder 每个 ≥ 1 次（清单是"（无必插）"时不强制任何 folder）
3. 单段时长：5s 素材 ≥ 2.5s，3s 素材 ≥ 1.5s
4. 任意两段 [s1,e1][s2,e2] 不区间重叠（重叠的合并或缩短）
5. **center 模式额外约束**：头 0-20% 和 尾 80-100% **原则无素材**，但字幕明确命中具体地点词（如"下楼就是 / 出门 200 米"）的段允许保留

═══════════════════════════════════════
📤 输出 JSON
═══════════════════════════════════════
{{
  "revisions": [
    {{"idx": 0, "action": "keep"}},
    {{"idx": 1, "action": "shorten", "new_end": 7.5}},
    {{"idx": 2, "action": "discard"}},
    {{"idx": 3, "action": "move", "new_start": 6.0}}
  ],
  "summary": "本轮删 X 段 + 缩 Y 段 + 移 Z 段，从 Ns 降到 Ms"
}}

只返回 JSON，不要其他文字。"""
        return prompt

    def _second_review(self, client, insertions, subtitles, duration, target_min, target_max, mat_pct, coverage_mode):
        """v2.10.25 第二层 LLM 审核（构造 prompt → 调 LLM → 返回 revisions）

        Returns:
            list: revisions 列表，形如 [{"idx": 0, "action": "keep"}, ...]
                  出错/解析失败时返回空列表（fallback 到第一层结果）
        """
        coverage_mode_desc = "自由分布" if coverage_mode == "free" else "双端固定（素材集中在中间，两头主播）"
        prompt = self._build_second_review_prompt(
            insertions, subtitles, duration,
            target_min=target_min, target_max=target_max,
            mat_pct=mat_pct, coverage_mode=coverage_mode,
            coverage_mode_desc=coverage_mode_desc,
        )
        try:
            result = client.chat_json(prompt)
        except Exception as e:
            self._log(f"  ⚠️ 第二层 LLM 失败 (fallback 到第一层结果): {e}")
            return []

        revisions = result if isinstance(result, list) else result.get("revisions", [])
        summary = ""
        if isinstance(result, dict):
            summary = result.get("summary", "")
        if summary:
            self._log(f"  📝 第二层总结: {summary}")
        self._log(f"  📋 第二层返回 {len(revisions)} 条修订")
        for r in revisions[:5]:
            self._log(f"     • idx={r.get('idx')} action={r.get('action')} new_end={r.get('new_end', '-')}")
        if len(revisions) > 5:
            self._log(f"     ... + {len(revisions) - 5} 条")
        return revisions

    def _apply_revisions(self, insertions, revisions):
        """v2.10.25 应用 revisions 到 insertions

        Args:
            insertions: 第一层 LLM 输出的 insertions
            revisions: 第二层 LLM 输出的 revisions (list of {idx, action, new_end})

        Returns:
            list: 应用修订后的 insertions（被 discard 的段删除，被 shorten 的段缩短）
        """
        if not revisions:
            return insertions
        # 按 idx 建索引，处理 LLM 漏报/多报
        rev_by_idx = {}
        for r in revisions:
            idx = r.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(insertions):
                rev_by_idx[idx] = r

        result = []
        stats = {"keep": 0, "shorten": 0, "discard": 0, "move": 0}
        for i, ins in enumerate(insertions):
            rev = rev_by_idx.get(i)
            if rev is None:
                # LLM 没提到的段，默认 keep
                result.append(ins)
                stats["keep"] += 1
                continue
            action = rev.get("action", "keep")
            if action == "discard":
                stats["discard"] += 1
                self._log(f"  🗑️ 第二层删 idx={i} [{ins.get('folder_label', '?')}]")
                continue
            if action == "shorten":
                try:
                    new_end = float(rev.get("new_end"))
                    s = float(ins.get("start", 0))
                    # 至少保留 2.5s (5s 素材) / 1.5s (3s 素材)
                    if new_end - s < 1.5:
                        new_end = s + 2.5
                    ins["end"] = round(new_end, 2)
                    stats["shorten"] += 1
                    self._log(f"  ✂️ 第二层缩 idx={i} → {ins['end']:.1f}s")
                    result.append(ins)
                    continue
                except (TypeError, ValueError):
                    # shorten 但 new_end 非法 → fallback 到 keep
                    result.append(ins)
                    stats["keep"] += 1
                    continue
            if action == "move":
                try:
                    new_start = float(rev.get("new_start"))
                    s = float(ins.get("start", 0))
                    e = float(ins.get("end", 0))
                    dur = e - s
                    # 中心区范围 20%-80%
                    center_lo = 0.2 * getattr(self, "_current_video_duration", 0) or 0
                    # 拿不到 _current_video_duration 时不强制范围，让 LLM 决定
                    if dur < 1.0:
                        # 时长太短不 move
                        result.append(ins)
                        stats["keep"] += 1
                        continue
                    # 移动：start 改 new_start，end 跟着平移（保持时长）
                    ins["start"] = round(new_start, 2)
                    ins["end"] = round(new_start + dur, 2)
                    stats["move"] += 1
                    self._log(f"  🚚 第二层移 idx={i} → [{ins['start']:.1f}-{ins['end']:.1f}]")
                    result.append(ins)
                    continue
                except (TypeError, ValueError):
                    # move 但 new_start 非法 → fallback 到 keep
                    result.append(ins)
                    stats["keep"] += 1
                    continue
            # action == "keep" 或未知
            result.append(ins)
            stats["keep"] += 1

        self._log(f"  🔧 应用修订: keep={stats['keep']}, shorten={stats['shorten']}, discard={stats['discard']}, move={stats['move']}")
        return result

    def _postprocess_insertions(self, insertions, mat_secs, duration, mat_pct, required_labels=None):
        """v2.10.17 客户端兜底（不依赖 LLM 守规则）

        4 步流水线:
          Step 1 - 合并连续同 folder（不管 LLM 给多少段，强制合并成 1 段）
          Step 2 - 时长对齐到素材秒数整数倍（向上取整，不浪费时长）
          Step 3 - 算术约束检查（覆盖率 ±0.5s）
          Step 4 - 超出视频时长截断

        Args:
            insertions: LLM 输出的插入点列表（已经被字段兜底过）
            mat_secs: {folder_label: 秒数} 字典（如 {"房子": 5, "爱琴海": 5}）
            duration: 主视频总时长（秒）
            mat_pct: 素材占比 0-100
            required_labels: 用户勾选「必插」⭐ 的 folder_label 集合（v2.10.42 新增：算术超标时优先删非必插段）

        Returns:
            处理后的插入点列表（守规则）
        """
        required_labels = required_labels or set()
        if not insertions:
            return insertions

        before_count = len(insertions)
        merged_log = []
        align_log = []
        truncate_log = []

        # ===== Step 1: 合并连续同 folder =====
        # 算法: 按 folder_label 分组，连续段合并（中间有别 folder 隔开则不合并）
        merged = []
        for ins in sorted(insertions, key=lambda x: x["start"]):
            label = ins.get("folder_label", "")
            s, e = ins["start"], ins["end"]
            if merged and merged[-1]["folder_label"] == label:
                # 连续同 folder → 扩 last.end 到 max(last.end, e)
                old_end = merged[-1]["end"]
                merged[-1]["end"] = max(old_end, e)
                merged_log.append(f"{label} {old_end:.1f}→{merged[-1]['end']:.1f}")
            else:
                merged.append(dict(ins))  # 复制避免改原对象
        insertions = merged

        # ===== Step 2: 时长对齐 + 长段拆段（v2.10.49 改：拆段凑满 cur + 段数自适应）=====
        # 旧算法（v2.10.45）：cur > sec 时拆 n 段，每段 sec，间隔 MIN_GAP_SPLIT=1.0s
        #   bug：cur=6.8s sec=3s → 拆 3 段 [11-14][15-18][19-?] 但第 2 段 end=18 > 17.8 截到 17.8 cur=2.8s
        #        第 3 段 start=19 > 17.8 直接 break → 只输出 2 段 [11-14]+[15-17.8]，没凑满 6.8s，还留 1s gap
        # 用户反馈（v2.10.49）："11.0-17.8 6.8s 素材只有 3s，需要三个素材插入凑整 6.8s，但三条不要有一条比例太低，比如 3+3+0.8"
        # 新算法（v2.10.49）：
        #   - cur > sec → 拆 n 段凑满 cur，**不留 gap**（紧贴）
        #   - n = ceil(cur/sec)，上限 5
        #   - 如果 cur/n < min_dur（每段太短），减少 n（让段数更少，每段更长）
        #   - 每段尽量均匀（接近 cur/n），最后一段 end = 原 end（吸收浮点误差）
        #   - 例: 3s 素材 + cur=6.8s → n=ceil(6.8/3)=3, avg=2.27 < min_dur=2.5 → n=2, 每段 3.4s
        #         3s 素材 + cur=8.0s → n=3, avg=2.67 ≥ 2.5 → 拆 3 段各 2.67s
        #         3s 素材 + cur=2.5s → 保持 2.5s（min_dur）
        #         3s 素材 + cur=2.0s → 拉到 3s（避免太短）
        new_insertions = []
        for ins in sorted(insertions, key=lambda x: x["start"]):
            label = ins.get("folder_label", "")
            sec = mat_secs.get(label, 3)  # 默认 3s
            cur = ins["end"] - ins["start"]
            if cur <= 0:
                continue  # 字段兜底已经处理了，跳过
            # min_dur = sec - 0.5（5s 素材允许 4.5s+；3s 素材允许 2.5s+）
            min_dur = max(sec - 0.5, 1.5)
            if cur <= sec and cur >= min_dur:
                # 时长合适：保留
                new_insertions.append(ins)
            elif cur < min_dur:
                # 太短：拉到 sec（保持每段至少 sec - 0.5s）
                new_end = round(ins["start"] + sec, 2)
                if new_end > duration:
                    new_end = round(duration, 2)
                if new_end - ins["start"] < 1.5:
                    # 拉不到 min_dur（视频太短）→ 放弃
                    align_log.append(f"丢 {label} {ins['start']:.1f}-{ins['end']:.1f}（拉不到 min_dur）")
                    continue
                new_ins = dict(ins)
                new_ins["end"] = new_end
                new_insertions.append(new_ins)
                align_log.append(f"{label} {ins['start']:.1f}-{ins['end']:.1f} ({cur:.1f}s) → {new_ins['start']:.1f}-{new_ins['end']:.1f} ({sec}s, 拉长)")
            else:
                # cur > sec：拆成 n 段凑满 cur，**无 gap**（v2.10.49 改）
                n = max(1, int((cur + sec - 0.01) // sec))  # ceil
                # 限制最大拆段数（避免一镜到底拆出 10 段）
                n = min(n, 5)
                # v2.10.49 新增：检查每段是否 ≥ min_dur，avg < min_dur 就减少 n（避免出现 0.8s 短段）
                while n > 1 and cur / n < min_dur:
                    n -= 1
                split_count = 0
                for i in range(n):
                    seg_start = round(ins["start"] + i * (cur / n), 2)
                    if i == n - 1:
                        # 最后一段 end = 原 end（凑满，吸收浮点误差）
                        seg_end = round(ins["end"], 2)
                    else:
                        seg_end = round(ins["start"] + (i + 1) * (cur / n), 2)
                    if seg_end - seg_start < 1.5:
                        break  # 太短就停
                    if seg_end > duration:
                        seg_end = round(duration, 2)
                    new_ins = dict(ins)
                    new_ins["start"] = seg_start
                    new_ins["end"] = seg_end
                    new_insertions.append(new_ins)
                    split_count += 1
                if split_count > 0:
                    if split_count == 1:
                        align_log.append(f"{label} {ins['start']:.1f}-{ins['end']:.1f} ({cur:.1f}s) → 拆 1 段（{seg_end - seg_start:.1f}s）")
                    else:
                        align_log.append(f"{label} {ins['start']:.1f}-{ins['end']:.1f} ({cur:.1f}s) → 拆 {split_count} 段（≈{cur/n:.1f}s/段, 凑满 cur）")
        insertions = new_insertions

        # ===== v2.10.55 去掉 v2.10.50 加的"拆段后再合并"段 =====
        # 原意图：拆段后相邻同 folder 合并成 1 段让 UI 清爽
        # 实际 bug：v2.10.49 拆 [1.2-7.5] cur=6.3s（sec=3s）→ 拆成 [1.2-4.35][4.35-7.5] 两段
        #   → 拆段后合并检测同 folder + 紧贴 → 合并回 [1.2-7.5] → 又变 6.3s 单段
        # 修法：去掉这段合并，让 Step 2 拆段结果直接透传到 UI，LLM 真实输出可见
        # 注：Step 1（line 2734-2747）的合并保留，它是处理 LLM 给的连续段，不是处理拆段后的状态

        # ===== Step 3: 算术约束检查 =====
        # 算总素材时长，目标 = duration × mat_pct / 100，容差 ±0.5s
        total = sum(ins["end"] - ins["start"] for ins in insertions)
        target = duration * mat_pct / 100
        target_min = target - 0.5
        target_max = target + 0.5

        arithmetic_log = ""
        if total > target_max:
            # v2.10.42: 强化算术兜底——能缩就缩，缩不动就删（非必插段优先）
            arithmetic_log = f"⚠️ 算术超标 {total:.2f}s > {target_max:.2f}s（目标 {target:.2f}s）→ 强制缩到 target 区间"
            while total > target_max + 0.01 and insertions:
                # 优先处理非必插段（必插段放最后）
                non_req = [x for x in insertions if x.get("folder_label", "") not in required_labels]
                req = [x for x in insertions if x.get("folder_label", "") in required_labels]
                # 优先非必插；如果非必插空了再用必插
                pool = non_req if non_req else req
                if not pool:
                    arithmetic_log += "（无可处理段，退出）"
                    break
                longest = max(pool, key=lambda x: x["end"] - x["start"])
                label = longest.get("folder_label", "")
                is_required = label in required_labels
                sec = mat_secs.get(label, 3)
                cur_dur = longest["end"] - longest["start"]
                excess = total - target_max
                # 最短时长底线 = sec + 0.5（如 5s 素材最低到 5.5s，下面再短就强制 discard）
                min_dur = sec + 0.5
                if cur_dur > min_dur and excess > 0:
                    # 还能缩：缩掉 min(超出的量, 当前时长-最短底线)
                    shrink = min(excess, cur_dur - min_dur)
                    new_end = round(longest["end"] - shrink, 2)
                    if new_end <= longest["start"] + 1.0:
                        new_end = round(longest["start"] + min_dur, 2)
                    longest["end"] = new_end
                    arithmetic_log += f" [{label} {cur_dur:.1f}→{longest['end']-longest['start']:.1f}s]"
                else:
                    # 已是最短：删
                    if is_required:
                        arithmetic_log += f" [⚠️ 必插段 {label} 受保护，不再处理]"
                        # 必插段不能再动，从 pool 摘出去避免死循环
                        break
                    insertions.remove(longest)
                    arithmetic_log += f" [删 {label}]"
                total = sum(ins["end"] - ins["start"] for ins in insertions)
            if total > target_max + 0.01:
                arithmetic_log += f" ⚠️ 最终仍超 {total:.2f}s（可能必插段过多，建议减少必插数）"
        elif total < target_min:
            # v2.10.50 升级：算术不足时给用户更明显的警告（主播出镜会更久）
            shortfall = target_min - total
            arithmetic_log = f"⚠️ 算术不足 {total:.2f}s < {target_min:.2f}s（目标 {target:.2f}s, mat_pct={mat_pct}%）— 差 {shortfall:.1f}s 素材，主播会出镜更久（建议：a) 增加素材段数；b) 提高 mat_pct 配置；c) 减少必插段）"

        # ===== Step 4: 超出视频时长截断（v2.10.50 加：start < 0 也截断） =====
        for ins in insertions:
            if ins["end"] > duration:
                truncate_log.append(f"{ins.get('folder_label', '')} {ins['end']:.1f}→{duration:.1f}")
                ins["end"] = round(duration, 2)
            if ins["start"] < 0:  # v2.10.50 新增：LLM 给负数 start 时截断到 0
                truncate_log.append(f"{ins.get('folder_label', '')} start {ins['start']:.1f}→0")
                ins["start"] = 0
            if ins["start"] >= ins["end"]:
                # 兜底：start >= end 视为无效
                truncate_log.append(f"{ins.get('folder_label', '')} start>=end 删除")

        # 过滤掉 start>=end 的无效段
        insertions = [ins for ins in insertions if ins["start"] < ins["end"]]

        # ===== Step 5: 几何重叠修正 + snap 到首尾相接（v2.10.50 修：last_end=None 避免首段 snap）=====
        # 解决 LLM 不守硬约束："任意两段 [s1,e1][s2,e2] 不区间重叠"
        # 旧 bug（用户截图）：
        #   #1 [5.0-8.0] #2 [8.0-11.0] 首尾相接（gap=0）✓ 保留
        #   #2→#3 gap 0.2s（用户指出"宁可【8.0-11.0][11.0-14.0]"——要 snap 且 cur 保持）
        #   #3 [11.2-14.2] #4 [13.8-16.8] 几何重叠 0.4s → 修紧贴
        # v2.10.46 算法：只处理重叠，gap>0 保留
        # v2.10.47 算法：加 snap——gap < sec 时把后段 start 拉到前段 end，但 end 不动 → 出现 [11.0-14.2] cur=3.2s（cur 多了 0.2s）
        # v2.10.48 算法：snap 时 start 和 end 都调整，cur 保持原值 → [11.0-14.0] cur=3.0s（更整齐）
        # v2.10.50 修复：last_end 初始化改成 None（旧版 0.0 导致 first.start<sec 的段被 snap 到 [0,cur]）
        #   - 重叠 → 后段 start 拉到 前段 end（end 不动，紧贴）
        #   - 0 < gap < sec（接近首尾相接）→ snap 到前段 end，cur 保持原值（end 也调）
        #   - snap 后 end 超 video 边界 → 不 snap
        #   - gap >= sec（真有大段主播时间）→ 保留（不 snap）
        #   - 第一段没前段（last_end=None）→ 跳过重叠/snap 判断，原样保留
        sorted_ins = sorted(insertions, key=lambda x: x["start"])
        fixed = []
        gap_log = []
        last_end = None  # v2.10.50 修复：原 0.0 引发首段 snap 到 [0,cur]
        for ins in sorted_ins:
            label = ins.get("folder_label", "")
            sec = mat_secs.get(label, 3)
            s_orig, e_orig = ins["start"], ins["end"]
            if last_end is not None:  # v2.10.50 修复：仅当存在前段才判断
                if s_orig < last_end:  # 真重叠
                    new_s = round(last_end, 2)
                    if new_s + 1.5 > e_orig:  # 挤不到 1.5s
                        gap_log.append(f"删 {label} {s_orig:.1f}-{e_orig:.1f}（与前段重叠且挤不够 1.5s）")
                        continue
                    gap_log.append(f"修重叠 {label} {s_orig:.1f}-{e_orig:.1f} → {new_s:.1f}-{e_orig:.1f}（紧贴前段）")
                    ins["start"] = new_s
                elif 0 < s_orig - last_end < sec:  # snap
                    new_s = round(last_end, 2)
                    cur_orig = e_orig - s_orig
                    new_e = round(new_s + cur_orig, 2)
                    if new_e <= duration + 0.01:  # 不超 video 边界
                        gap_log.append(f"snap {label} {s_orig:.1f}-{e_orig:.1f} → {new_s:.1f}-{new_e:.1f}（gap {s_orig-last_end:.1f}s<{sec}s，cur 保持 {cur_orig:.1f}s）")
                        ins["start"] = new_s
                        ins["end"] = new_e
                    # else: new_e 超界 → 不 snap
            # gap >= sec 或 第一段（last_end is None）→ 保留原样
            fixed.append(ins)
            last_end = ins["end"]
        insertions = fixed

        # ===== 汇总日志 =====
        if merged_log:
            self._log(f"  🔗 合并 {len(merged_log)} 段连续同 folder: {'; '.join(merged_log[:5])}")
        if align_log:
            self._log(f"  📐 时长对齐 {len(align_log)} 段: {'; '.join(align_log[:5])}")
        if gap_log:
            self._log(f"  ↔️ 几何修正 {len(gap_log)} 段: {'; '.join(gap_log[:5])}")
        if arithmetic_log:
            self._log(f"  {arithmetic_log}")
        if truncate_log:
            self._log(f"  ✂️ 截断 {len(truncate_log)} 段: {'; '.join(truncate_log[:5])}")

        after_count = len(insertions)
        if before_count != after_count:
            self._log(f"  📊 兜底后: {before_count} 段 → {after_count} 段")

        return insertions

    # ---- LLM（v1.13 mock，已弃用，保留作 fallback）----
    def _llm_legacy_mock(self, video_path, subtitles):
        random.seed(hash(video_path) & 0xffffffff)
        enabled_mats = [m for m in self.material_folders if m["enabled"]]
        if not enabled_mats or not subtitles:
            return []

        # v2.10.10 mock fallback：preset_mode 已删除，仅保留"LLM 自决"随机分配
        insertions = []
        n = random.randint(1, min(3, len(subtitles)))
        chosen = sorted(random.sample(range(len(subtitles)), n))
        for idx in chosen:
            sub = subtitles[idx]
            m = random.choice(enabled_mats)
            mat_sec = m.get("seconds", 5)
            ins_dur = min(mat_sec, sub["end"] - sub["start"])
            mode_choice = random.choice(["small", "large"])
            if mode_choice == "large":
                end_idx = min(idx + random.randint(1, 3), len(subtitles) - 1)
                end_t = subtitles[end_idx]["end"]
                ins_dur = min(end_t - sub["start"], mat_sec * 3)
                ins_end = round(sub["start"] + ins_dur, 1)
            else:
                end_idx = idx
                ins_end = round(sub["start"] + ins_dur, 1)
            insertions.append({
                "start": round(sub["start"], 1),
                "end": ins_end,
                "position": "replaced",
                "folder_label": m["label"],
                "mode": mode_choice,
                "reason": f"mock fallback: LLM 自决 - {m['label']}",
                "subtitle_start_idx": idx,
                "subtitle_end_idx": end_idx if mode_choice == "large" else idx,
                "material_seconds": mat_sec,
            })
        return insertions

    def _extract_materials_for_insertions(self, insertions):
        """从每个 folder_label 对应文件夹抽 1 个视频文件（按抽取方式）
        v1.41：跨视频去重 — 同一素材不会被多条主视频复用
        """
        materials = []
        for ins in insertions:
            label = ins.get("folder_label", "")
            mf = next((m for m in self.material_folders if m["label"] == label), None)
            if not mf or not os.path.isdir(mf["path"]):
                materials.append(None)
                continue
            try:
                files = [f for f in os.listdir(mf["path"]) if f.lower().endswith(VIDEO_EXTS) and not f.startswith("._")]
                if not files:
                    materials.append(None)
                    continue

                # v1.41：加锁抽素材，确保多 worker 并发时不重复
                with self._extract_lock:
                    # 优先选未用过的素材
                    unused = [f for f in files
                              if os.path.join(mf["path"], f) not in self._used_material_paths]
                    if not unused:
                        # 该 folder 全部素材都用过了 → 重置该 folder 的去重集合
                        self._log(f"  ⚠️ [{label}] {len(files)} 个素材已全部用完，重置该 folder 去重")
                        # 只清掉该 folder 路径的标记，其他 folder 的保留
                        prefix = mf["path"] + os.sep
                        self._used_material_paths = {p for p in self._used_material_paths if not p.startswith(prefix)}
                        unused = files

                    if EXTRACT_OPTIONS_KEY_BY_DISPLAY.get(self.extract_mode_var.get(), "random") == "sequential":  # v1.47: 反查 display→key
                        # 顺序：用 hash 固定选第 N 个（保证同一 video 内多次抽同一 folder 时稳定）
                        idx = abs(hash(label + str(len(unused)))) % len(unused)
                        chosen = unused[idx]
                    else:
                        chosen = random.choice(unused)
                    chosen_path = os.path.join(mf["path"], chosen)
                    self._used_material_paths.add(chosen_path)
                materials.append(chosen_path)
                self._log(f"  🎲 抽 [{label}] → {chosen}")
            except Exception:
                materials.append(None)
        return materials

    # ================================================================
    # 推 Tab 5
    # ================================================================
    def _push_one_to_tab5(self, q):
        if not q.get("insertions"):
            self._log(f"⚠️ {os.path.basename(q['video_path'])} 没有插入点，跳过推送")
            return
        if not q.get("materials"):
            self._log(f"⚠️ {os.path.basename(q['video_path'])} 没有抽取到素材，跳过推送")
            return

        # v2.10.41 推 Tab 5 前兜底：folder_label 必须在 all_labels 里（_llm_real 已翻译，理论必中）
        all_labels = [mf.get("label", "") for mf in self.material_folders if mf.get("label")]
        fixed_count = 0
        for ins in q.get("insertions", []):
            llm_label = ins.get("folder_label", "") or ""
            if not all_labels or llm_label in all_labels:
                continue
            self._log(f"  ⚠️ folder_label 异常: {llm_label!r}，回退到 {all_labels[0]!r}")
            ins["folder_label"] = all_labels[0]
            fixed_count += 1
        if fixed_count:
            self._log(f"  🔧 推 Tab 5 前修复 {fixed_count} 个 folder_label")

        # v2.07：单条推送前预估素材是否够用
        warnings = self._check_supply([q])
        if warnings:
            head = "\n".join(warnings[:6])
            more = f"\n...还有 {len(warnings)-6} 条" if len(warnings) > 6 else ""
            if not messagebox.askyesno(
                "⚠️ 素材可能不够",
                f"「{os.path.basename(q['video_path'])}」预测素材需求与现有量对比：\n\n{head}{more}\n\n是否仍继续推送？",
            ):
                self._log(f"⏸️ 用户取消推送：{os.path.basename(q['video_path'])}（素材不足）")
                return

        # 构造 payload
        payload = {
            "video_path": q["video_path"],
            "insertions": q["insertions"],
            "materials": q["materials"],
            "main_video_dir": self.video_folder_var.get().strip() if hasattr(self, "video_folder_var") else "",  # v1.49
            "exec_config": {
                "extract_mode": self.extract_mode_var.get(),
                "mute_material": self.mute_material_var.get(),
                "layout": self.layout_var.get(),
                "pip_size": self.pip_size_var.get(),
                "pip_margin": self.pip_margin_var.get(),
                "cleanup_mode": self.cleanup_mode_var.get(),
                "main_video_cleanup": self.main_video_cleanup_var.get(),  # v1.40
            },
            # v1.21：AI 输出目录 + 后缀（避免和手动混剪混淆）
            "ai_output_dir": self.ai_output_dir.get().strip() or "/Users/zxz/Desktop/口播成品",
            "output_suffix": "_AI",
            # v2.10.14 新增：输出分辨率（推到 Tab 5, 默认 1080P）
            "output_resolution": self.output_resolution_var.get(),
        }

        # 调用 merge_tab 的接收方法
        merge_tab = getattr(self.app, "merge_tab", None)
        if merge_tab is None:
            self._log("❌ Tab 5 (merge_tab) 未找到")
            return
        if hasattr(merge_tab, "load_from_ai_tab"):
            try:
                merge_tab.load_from_ai_tab(payload)
                q["pushed"] = True
                self._log(f"📤 已推 Tab 5：{os.path.basename(q['video_path'])}")
                self._refresh_queue_table()
            except Exception as e:
                self._log(f"❌ 推 Tab 5 失败：{e}")
        else:
            self._log("⚠️ Tab 5 还没有 load_from_ai_tab 方法（v1.13 阶段 3 才接）")
            # v1.13 阶段 1 暂存到内存，给阶段 3 接
            if not hasattr(self, "_pending_payloads"):
                self._pending_payloads = []
            self._pending_payloads.append(payload)
            q["pushed"] = True
            self._log(f"📥 已暂存（阶段 3 接 Tab 5）：{os.path.basename(q['video_path'])}")
            self._refresh_queue_table()

    def _auto_review_enqueue(self, entry):
        """v1.32 自动审核：把 entry 入队到 _ai_batch_jobs，触发自动执行。
        解决只推不跑导致 Tab 5 没主视频的问题——直接走串行批跑，
        每个视频独立临时目录，自动设置 main_video_dir/output_dir。
        """
        if not entry.get("insertions"):
            return

        if not hasattr(self, "_ai_batch_jobs"):
            self._ai_batch_jobs = []
            self._ai_batch_index = 0
            self._ai_batch_results = []

        job = {
            "video_path": entry["video_path"],
            "insertions": entry["insertions"],
            "materials": entry["materials"],
            "exec_config": {
                "extract_mode": self.extract_mode_var.get(),
                "mute_material": self.mute_material_var.get(),
                "layout": self.layout_var.get(),
                "pip_size": self.pip_size_var.get(),
                "pip_margin": self.pip_margin_var.get(),
                "cleanup_mode": self.cleanup_mode_var.get(),
                "main_video_cleanup": self.main_video_cleanup_var.get(),  # v1.40
            },
            "ai_output_dir": self.ai_output_dir.get().strip() or "/Users/zxz/Desktop/口播成品",
            "output_suffix": "_AI",
            # v2.10.14 新增：输出分辨率（推到 Tab 5, 默认 1080P）
            "output_resolution": self.output_resolution_var.get(),
        }
        self._ai_batch_jobs.append(job)
        self._log(
            f"⚡ 自动审核入队 [{len(self._ai_batch_jobs)}]："
            f"{os.path.basename(entry['video_path'])}"
        )
        # _ai_batch_run_next 内部有 running 标志，多个 entry 同时入队不会重复启动
        self._ai_batch_run_next()

    # ================================================================
    # 辅助
    # ================================================================
    def _create_tooltip(self, widget, text_fn):
        """v2.10.36：通用 tooltip 工具 —— 鼠标悬停显示提示文字。
        text_fn: callable 返回字符串（延迟计算，可显示动态内容）"""
        tip_window = [None]

        def _on_enter(event):
            text = text_fn() if callable(text_fn) else str(text_fn)
            if not text:
                return
            # 简单实现：用顶层 Toplevel 浮动标签（不依赖第三方 tooltip 库）
            try:
                if tip_window[0] is not None:
                    tip_window[0].destroy()
                tw = tk.Toplevel(widget)
                tw.wm_overrideredirect(True)  # 无边框
                tw.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 18}")
                lbl = tk.Label(tw, text=text, background="#333", foreground="#fff",
                               relief="solid", borderwidth=1, font=("", 9),
                               padx=6, pady=3, wraplength=400)
                lbl.pack()
                tip_window[0] = tw
            except Exception:
                pass

        def _on_leave(event):
            if tip_window[0] is not None:
                try:
                    tip_window[0].destroy()
                except Exception:
                    pass
                tip_window[0] = None

        widget.bind("<Enter>", _on_enter)
        widget.bind("<Leave>", _on_leave)
        widget.bind("<ButtonPress>", _on_leave)  # 点击关闭

    def _log(self, msg):
        """输出日志到主窗口日志区（如果 app 有 _log 方法）"""
        log_fn = getattr(self.app, "_log", None)
        if callable(log_fn):
            try:
                log_fn(f"[AI] {msg}")
            except Exception:
                pass
        # v1.47：同时写文件 debug log（stdout 在 dev 模式不可靠）
        try:
            with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[AI] {msg}\n")
        except Exception:
            pass

    def _dbg(self, msg):
        """只写文件 debug log（不进 GUI 日志，避免污染用户视图）"""
        try:
            import datetime as _dt
            ts = _dt.datetime.now().strftime("%H:%M:%S")
            with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [DBG-AI] {msg}\n")
        except Exception:
            pass


def build_ai_tab(parent, app):
    """入口函数：构造并返回 AITab 实例"""
    return AITab(parent, app)