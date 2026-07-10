import os
import sys
import re
import math
import json
import copy
import time
import random
import subprocess
import tempfile
import threading
import inspect
import shutil
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path
from datetime import datetime

# v1.75_debug: 打包环境下把所有 stdout/stderr 重定向到 ~/kobo_crash.log
# 闪退时这个文件就是诊断金矿
if getattr(sys, 'frozen', False):
    _log_path = os.path.expanduser('~/kobo_crash.log')
    try:
        _log_f = open(_log_path, 'a', buffering=1)
        _log_f.write(f"\n\n=== {datetime.now()} 启动 frozen={sys.frozen} ===\n")
        _log_f.write(f"sys.executable={sys.executable}\n")
        _log_f.write(f"sys.platform={sys.platform}\n")
        _log_f.write(f"sys._MEIPASS={getattr(sys, '_MEIPASS', None)}\n")
        sys.stdout = _log_f
        sys.stderr = _log_f
        # 全局异常也写日志
        def _excepthook(exc_type, exc_value, exc_tb):
            _log_f.write("\n!!! 未捕获异常 !!!\n")
            _log_f.write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
            _log_f.flush()
            sys.__excepthook__(exc_type, exc_value, exc_tb)
        sys.excepthook = _excepthook
    except Exception as _e:
        pass

import yaml
from queue import deque as _task_deque  # v1.44 全局任务队列

from kobo_engine import KoboEngine
from merge_tab import build_merge_tab
from ai_tab import build_ai_tab  # v1.13 Tab 6

try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pygame
    pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=512)
    HAS_PYGAME = True
except Exception:
    HAS_PYGAME = False


# ============================================================
# v1.44 全局任务队列（跨 Tab 互斥：先点先跑）
# ============================================================
class GlobalTaskQueue:
    """所有 Tab 的"开始"按钮共享一个队列。当前任务跑完才能跑下一个。"""

    def __init__(self, app):
        self.app = app
        self._lock = threading.Lock()
        self._current = None            # (tab_name, display, on_done)
        self._queue = _task_deque()     # [(tab_name, display, callback, on_done), ...]
        self._cancelled = set()         # 队列中被用户取消的 task_id

    def is_busy(self):
        with self._lock:
            return self._current is not None

    def current_label(self):
        with self._lock:
            if self._current is None:
                return None
            return self._current[1]

    def queue_size(self):
        with self._lock:
            return len(self._queue)

    def request(self, tab_name, display, callback, on_done=None):
        """请求执行任务。返回 ('started',) / ('queued', position) / ('rejected', reason)"""
        with self._lock:
            if self._current is None:
                self._current = (tab_name, display, on_done)
                self._emit_status()
                self.app.after(0, lambda: callback())
                return ("started",)
            else:
                self._queue.append((tab_name, display, callback, on_done))
                position = len(self._queue)
                self._emit_status()
                return ("queued", position)

    def release(self):
        """当前任务完成时调用。调度队列下一个。"""
        with self._lock:
            finished = self._current
            self._current = None
            # 触发 on_done 回调
            if finished is not None:
                _, display, on_done = finished
                if on_done is not None:
                    try:
                        self.app.after(0, on_done)
                    except Exception:
                        pass
            # 找下一个没被取消的
            while self._queue:
                tab_name, display, callback, on_done = self._queue.popleft()
                self._current = (tab_name, display, on_done)
                self._emit_status()
                self.app.after(0, lambda c=callback: c())
                return
            self._emit_status()

    def cancel_queued(self, tab_name):
        """取消所有等待中、来自指定 tab_name 的任务。返回取消数量。"""
        with self._lock:
            kept = _task_deque()
            cancelled = 0
            while self._queue:
                item = self._queue.popleft()
                if item[0] == tab_name:
                    cancelled += 1
                else:
                    kept.append(item)
            self._queue = kept
            self._emit_status()
            return cancelled

    def cancel_all_queued(self):
        """清空等待队列。返回取消数量。"""
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._emit_status()
            return count

    def set_busy(self, display):
        """v1.46：手动标记为忙碌（仅显示状态，不实际占用队列）。用于 AI 流水线等内部调度。"""
        with self._lock:
            self._current = (None, display, None)
            self._emit_status()

    def set_idle(self):
        """v1.46：手动清掉忙碌状态。如果队列里还有等待任务，自动调度下一个。"""
        with self._lock:
            if self._current is not None and self._current[2] is None:
                # 是 set_busy 标记的（on_done=None），直接清掉 + 调度队列下一个
                self._current = None
                self._emit_status()
                while self._queue:
                    tab_name, display, callback, on_done = self._queue.popleft()
                    self._current = (tab_name, display, on_done)
                    self._emit_status()
                    self.app.after(0, lambda c=callback: c())
                    return
                return
            elif self._current is not None:
                # 走正常 release 路径（已有队列调度逻辑）
                self.release()

    def _emit_status(self):
        try:
            self.app.after(0, self.app._refresh_global_status)
        except Exception:
            pass


APP_TITLE = "可乐口播视频生成器"
APP_VERSION = "2.10.57"  # ⬆️ v2.10.57 修 Win 端 3 个 bug：素材路径失效加载 + 人设删除按钮 + 一键清空所有配置

# ===== v1.12 ASR / LLM 默认值（写死，UI 不暴露）=====
LLM_TEMPERATURE = 0.3          # 让 LLM 老老实实出 JSON，别瞎发挥
LLM_MAX_TOKENS = 8192          # 防 JSON 被截断
LLM_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"   # 不用带 thinking 的新款
ASR_DEFAULT_MODEL = "TeleAI/TeleSpeechASR"
ASR_DEFAULT_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
LLM_DEFAULT_URL = "https://api.siliconflow.cn/v1/chat/completions"
CONFIG_FILE = "config.yaml"
PERSONAS_FILE = "personas.json"
LOCK_FILE = os.path.join(tempfile.gettempdir(), "kobo_video_generator.lock")
MAX_BATCH = 40  # ⬆️ v2.10.31 从 20 改到 40
THUMB_SIZE = (40, 40)
BASE_TOTAL_LINES = 30


def _app_dir():
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    base = os.path.abspath(__file__)
    return os.path.dirname(base)

def _get_ffmpeg_path():
    """获取 ffmpeg 路径（兼容打包环境 + dev 环境 + 系统 PATH）
    v1.72 修复：PyInstaller 把 ffmpeg 打成 bin/ffmpeg/ffmpeg（目录包裹，类似 .app bundle），旧逻辑找不到
    """
    ffmpeg_name = "ffmpeg"
    if sys.platform == "win32":
        ffmpeg_name += ".exe"

    if hasattr(sys, '_MEIPASS'):
        meipass_bin = os.path.join(sys._MEIPASS, 'bin')
        # 优先 1：直接文件 bin/ffmpeg
        direct = os.path.join(meipass_bin, ffmpeg_name)
        if os.path.isfile(direct):
            return direct
        # 优先 2：目录包裹 bin/ffmpeg/ffmpeg（PyInstaller 打包 ffmpeg.app bundle 风格）
        nested = os.path.join(meipass_bin, ffmpeg_name, ffmpeg_name)
        if os.path.isfile(nested):
            return nested
        # 优先 3：ffmpeg 在 bin/ 下没被打进（spec 没配），退回系统 PATH
        return shutil.which(ffmpeg_name) or ffmpeg_name
    # 开发环境：用项目本地 bin/ 或系统 PATH
    project_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin')
    # 优先 0：临时测试 ffmpeg 软切换（v2.10.16 调试用，存在 .bak_tessus 就用）
    tessus_path = os.path.join(project_bin, ffmpeg_name + '.bak_tessus')
    if os.path.isfile(tessus_path):
        return tessus_path
    direct = os.path.join(project_bin, ffmpeg_name)
    if os.path.isfile(direct):
        return direct
    nested = os.path.join(project_bin, ffmpeg_name, ffmpeg_name)
    if os.path.isfile(nested):
        return nested
    return shutil.which(ffmpeg_name) or ffmpeg_name


def _probe_duration(path):
    """探测音频/视频时长（秒）。优先 ffprobe，没有就用 ffmpeg -i 解析"""
    ffmpeg_exe = _get_ffmpeg_path()
    # v1.72：ffprobe 路径 = 同目录下的 ffprobe（兼容 bin/ffmpeg/ffmpeg 这种目录包裹结构）
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    ffmpeg_basename = os.path.basename(ffmpeg_exe)
    ffprobe_basename = "ffprobe.exe" if ffmpeg_basename.endswith(".exe") else "ffprobe"
    ffprobe_sibling = os.path.join(ffmpeg_dir, ffprobe_basename)

    candidates = [
        ffprobe_sibling,
        shutil.which("ffprobe") or "",
        shutil.which(ffmpeg_exe) or "",
    ]
    for exe in candidates:
        if not exe or not os.path.exists(exe):
            continue
        try:
            if exe.endswith("ffprobe") or exe.endswith("ffprobe.exe"):
                r = subprocess.run(
                    [exe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", path],
                    capture_output=True, text=True, timeout=10, start_new_session=True,  # v2.10.33: 脱离父进程 session
                )
                if r.returncode == 0 and r.stdout.strip():
                    return float(r.stdout.strip())
            else:  # 用 ffmpeg 解析时长
                r = subprocess.run(
                    [exe, "-i", path],
                    capture_output=True, text=True, timeout=10, start_new_session=True,  # v2.10.33: 脱离父进程 session
                )
                out = (r.stderr or "") + (r.stdout or "")
                import re
                m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
                if m:
                    h, mi, s = m.groups()
                    return int(h) * 3600 + int(mi) * 60 + float(s)
        except Exception:
            continue
    return None


class PersonaDialog(tk.Toplevel):
    def __init__(self, parent, persona_data=None, persona_name=""):
        super().__init__(parent)
        self.result = None
        self.transient(parent)
        self.grab_set()
        self.title("编辑人设" if persona_data else "新增人设")
        self.geometry("660x780")
        self.resizable(True, True)

        self.images = []
        self.audios = []
        self.videos = []
        self._thumb_refs = []

        if persona_data:
            self.name_var = tk.StringVar(value=persona_name)
            self.images = [dict(i) for i in persona_data.get("images", [])]
            self.audios = [dict(a) for a in persona_data.get("audios", [])]
            self.videos = [dict(v) for v in persona_data.get("videos", [])]
        else:
            self.name_var = tk.StringVar()

        self._build_ui()
        self._refresh_images()
        self._refresh_videos()
        self._refresh_audios()

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.wait_window()

    def _build_ui(self):
        main = ttk.Frame(self, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        ttk.Label(main, text="人设名称:").grid(row=0, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.name_var, width=30).grid(row=0, column=1, sticky=tk.EW, pady=5, padx=5)

        ttk.Separator(main, orient=tk.HORIZONTAL).grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=10)

        img_frame = ttk.LabelFrame(main, text="形象图片（勾选=参与随机，只勾1个=固定）", padding=5)
        img_frame.grid(row=2, column=0, columnspan=2, sticky=tk.NSEW, pady=5)

        img_container = ttk.Frame(img_frame)
        img_container.pack(fill=tk.BOTH, expand=True)

        self.img_canvas = tk.Canvas(img_container, height=160, highlightthickness=0)
        img_scroll = ttk.Scrollbar(img_container, orient=tk.VERTICAL, command=self.img_canvas.yview)
        self.img_list_frame = ttk.Frame(self.img_canvas)
        self.img_list_frame.bind("<Configure>", lambda e: self.img_canvas.configure(scrollregion=self.img_canvas.bbox("all")))
        self._img_canvas_win = self.img_canvas.create_window((0, 0), window=self.img_list_frame, anchor="nw")
        self.img_canvas.configure(yscrollcommand=img_scroll.set)
        self.img_canvas.bind("<Configure>", lambda e: self.img_canvas.itemconfig(self._img_canvas_win, width=e.width))
        self.img_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        img_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        img_btn_frame = ttk.Frame(img_frame)
        img_btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(img_btn_frame, text="添加图片", command=self._add_image).pack(side=tk.LEFT, padx=5)

        ttk.Separator(main, orient=tk.HORIZONTAL).grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=10)

        video_frame = ttk.LabelFrame(main, text="形象视频（勾选=参与随机，只勾1个=固定；部分模式需要视频而非图片）", padding=5)
        video_frame.grid(row=4, column=0, columnspan=2, sticky=tk.NSEW, pady=5)

        video_container = ttk.Frame(video_frame)
        video_container.pack(fill=tk.BOTH, expand=True)

        self.video_canvas = tk.Canvas(video_container, height=100, highlightthickness=0)
        video_scroll = ttk.Scrollbar(video_container, orient=tk.VERTICAL, command=self.video_canvas.yview)
        self.video_list_frame = ttk.Frame(self.video_canvas)
        self.video_list_frame.bind("<Configure>", lambda e: self.video_canvas.configure(scrollregion=self.video_canvas.bbox("all")))
        self._video_canvas_win = self.video_canvas.create_window((0, 0), window=self.video_list_frame, anchor="nw")
        self.video_canvas.configure(yscrollcommand=video_scroll.set)
        self.video_canvas.bind("<Configure>", lambda e: self.video_canvas.itemconfig(self._video_canvas_win, width=e.width))
        self.video_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        video_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        video_btn_frame = ttk.Frame(video_frame)
        video_btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(video_btn_frame, text="添加视频", command=self._add_video).pack(side=tk.LEFT, padx=5)

        ttk.Separator(main, orient=tk.HORIZONTAL).grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=10)

        audio_frame = ttk.LabelFrame(main, text="样本声音（勾选=参与随机，只勾1个=固定）", padding=5)
        audio_frame.grid(row=6, column=0, columnspan=2, sticky=tk.NSEW, pady=5)

        audio_container = ttk.Frame(audio_frame)
        audio_container.pack(fill=tk.BOTH, expand=True)

        self.audio_canvas = tk.Canvas(audio_container, height=100, highlightthickness=0)
        audio_scroll = ttk.Scrollbar(audio_container, orient=tk.VERTICAL, command=self.audio_canvas.yview)
        self.audio_list_frame = ttk.Frame(self.audio_canvas)
        self.audio_list_frame.bind("<Configure>", lambda e: self.audio_canvas.configure(scrollregion=self.audio_canvas.bbox("all")))
        self._audio_canvas_win = self.audio_canvas.create_window((0, 0), window=self.audio_list_frame, anchor="nw")
        self.audio_canvas.configure(yscrollcommand=audio_scroll.set)
        self.audio_canvas.bind("<Configure>", lambda e: self.audio_canvas.itemconfig(self._audio_canvas_win, width=e.width))
        self.audio_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        audio_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        audio_btn_frame = ttk.Frame(audio_frame)
        audio_btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(audio_btn_frame, text="添加声音", command=self._add_audio).pack(side=tk.LEFT, padx=5)

        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=7, column=0, columnspan=2, pady=15)
        ttk.Button(btn_frame, text="保存", command=self._on_save).pack(side=tk.LEFT, padx=10, ipadx=20)
        ttk.Button(btn_frame, text="取消", command=self._on_cancel).pack(side=tk.LEFT, padx=10, ipadx=20)

        main.columnconfigure(1, weight=1)
        main.rowconfigure(2, weight=1)
        main.rowconfigure(4, weight=1)
        main.rowconfigure(6, weight=1)

    def _make_thumbnail(self, path):
        if not HAS_PIL:
            return None
        try:
            img = Image.open(path)
            img.thumbnail(THUMB_SIZE)
            photo = ImageTk.PhotoImage(img)
            return photo
        except Exception:
            return None

    def _add_image(self):
        paths = filedialog.askopenfilenames(filetypes=[("图片", "*.png *.jpg *.jpeg *.PNG *.JPG *.JPEG")])
        for p in paths:
            self.images.append({"path": p, "checked": True})
        self._refresh_images()

    def _add_audio(self):
        paths = filedialog.askopenfilenames(filetypes=[("音频", "*.mp3 *.wav *.flac *.MP3 *.WAV *.FLAC")])
        for p in paths:
            self.audios.append({"path": p, "checked": True})
        self._refresh_audios()

    def _add_video(self):
        paths = filedialog.askopenfilenames(filetypes=[("视频", "*.mp4 *.mov *.avi *.mkv *.webm *.MP4 *.MOV *.AVI *.MKV *.WEBM")])
        for p in paths:
            self.videos.append({"path": p, "checked": True})
        self._refresh_videos()

    def _refresh_videos(self):
        for w in self.video_list_frame.winfo_children():
            w.destroy()
        for i, vid in enumerate(self.videos):
            row_frame = ttk.Frame(self.video_list_frame)
            row_frame.pack(fill=tk.X, pady=1, padx=2)
            var = tk.BooleanVar(value=vid["checked"])
            cb = ttk.Checkbutton(row_frame, variable=var, command=lambda idx=i, v=var: self._toggle_video(idx, v))
            cb.pack(side=tk.LEFT)
            name = os.path.basename(vid["path"])
            ttk.Label(row_frame, text=name, width=30).pack(side=tk.LEFT, padx=5)
            ttk.Button(row_frame, text="▶", width=3, command=lambda p=vid["path"]: self._open_video(p)).pack(side=tk.LEFT, padx=2)
            ttk.Button(row_frame, text="删除", width=4, command=lambda idx=i: self._del_video(idx)).pack(side=tk.RIGHT)

    def _open_video(self, path):
        if not os.path.exists(path):
            messagebox.showerror("错误", f"视频文件不存在:\n{path}", parent=self)
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("错误", f"无法打开: {e}", parent=self)

    def _toggle_video(self, idx, var):
        if idx < len(self.videos):
            self.videos[idx]["checked"] = var.get()
            self._on_edit_change()

    def _on_edit_change(self):
        """编辑对话框内任意 checkbox 改变：同步回 self.personas + 保存 + 刷新详情"""
        name = self.persona_name_var.get() if hasattr(self, "persona_name_var") else None
        if name and hasattr(self.master, "personas"):
            self.master.personas[name] = {
                "images": list(self.images),
                "videos": list(self.videos),
                "audios": list(self.audios),
            }
            self.master._save_personas()
            self.master._draw_persona_list()
            self.master._on_persona_select()

    def _del_video(self, idx):
        if idx < len(self.videos):
            del self.videos[idx]
            self._refresh_videos()

    def _refresh_images(self):
        for w in self.img_list_frame.winfo_children():
            w.destroy()
        self._thumb_refs = []
        for i, img in enumerate(self.images):
            row_frame = ttk.Frame(self.img_list_frame)
            row_frame.pack(fill=tk.X, pady=2, padx=2)

            thumb = self._make_thumbnail(img["path"])
            if thumb:
                lbl = ttk.Label(row_frame, image=thumb)
                lbl.pack(side=tk.LEFT, padx=(0, 5))
                self._thumb_refs.append(thumb)

            var = tk.BooleanVar(value=img["checked"])
            cb = ttk.Checkbutton(row_frame, variable=var, command=lambda idx=i, v=var: self._toggle_image(idx, v))
            cb.pack(side=tk.LEFT)
            name = os.path.basename(img["path"])
            ttk.Label(row_frame, text=name, width=30).pack(side=tk.LEFT, padx=5)
            ttk.Button(row_frame, text="删除", width=4, command=lambda idx=i: self._del_image(idx)).pack(side=tk.RIGHT)

    def _refresh_audios(self):
        for w in self.audio_list_frame.winfo_children():
            w.destroy()
        for i, aud in enumerate(self.audios):
            row_frame = ttk.Frame(self.audio_list_frame)
            row_frame.pack(fill=tk.X, pady=1, padx=2)
            var = tk.BooleanVar(value=aud["checked"])
            cb = ttk.Checkbutton(row_frame, variable=var, command=lambda idx=i, v=var: self._toggle_audio(idx, v))
            cb.pack(side=tk.LEFT)
            name = os.path.basename(aud["path"])
            ttk.Label(row_frame, text=name, width=30).pack(side=tk.LEFT, padx=5)
            play_btn = ttk.Button(row_frame, text="▶", width=3)
            play_btn.configure(command=lambda p=aud["path"], b=play_btn: self._play_audio(p, b))
            play_btn.pack(side=tk.LEFT, padx=2)
            ttk.Button(row_frame, text="删除", width=4, command=lambda idx=i: self._del_audio(idx)).pack(side=tk.RIGHT)

    def _play_audio(self, path, btn):
        if not os.path.exists(path):
            messagebox.showerror("错误", f"声音文件不存在:\n{path}", parent=self)
            return
        try:
            if HAS_PYGAME:
                if pygame.mixer.music.get_busy() and getattr(btn, '_playing_path', None) == path:
                    pygame.mixer.music.stop()
                    btn.configure(text="▶")
                    btn._playing_path = None
                    return
                pygame.mixer.music.stop()
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
                btn.configure(text="⏸")
                btn._playing_path = path
                self.after(200, lambda: self._watch_playback(btn, path))
            else:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("错误", f"无法播放: {e}", parent=self)

    def _watch_playback(self, btn, path):
        if not pygame.mixer.music.get_busy():
            btn.configure(text="▶")
            btn._playing_path = None
            return
        if getattr(btn, '_playing_path', None) == path:
            self.after(200, lambda: self._watch_playback(btn, path))
        else:
            btn.configure(text="▶")

    def _toggle_image(self, idx, var):
        if idx < len(self.images):
            self.images[idx]["checked"] = var.get()
            self._on_edit_change()

    def _toggle_audio(self, idx, var):
        if idx < len(self.audios):
            self.audios[idx]["checked"] = var.get()
            self._on_edit_change()

    def _del_image(self, idx):
        if idx < len(self.images):
            del self.images[idx]
            self._refresh_images()

    def _del_audio(self, idx):
        if idx < len(self.audios):
            del self.audios[idx]
            self._refresh_audios()

    def _on_save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "请输入人设名称", parent=self)
            return
        checked_imgs = [i for i in self.images if i["checked"]]
        checked_auds = [a for a in self.audios if a["checked"]]
        if not checked_imgs:
            messagebox.showwarning("提示", "请至少勾选1张形象图片", parent=self)
            return
        if not checked_auds:
            messagebox.showwarning("提示", "请至少勾选1个样本声音", parent=self)
            return
        self.result = {
            "name": name,
            "data": {
                "images": self.images,
                "videos": self.videos,
                "audios": self.audios,
            }
        }
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


class KoboApp:
    def __init__(self):
        self._kill_stale_processes()

        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")
        self.root.geometry("1100x800")
        self.root.resizable(False, False)
        # 居中显示（避免窗口被推到屏幕外导致可视范围问题）
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - 1100) // 2)
        y = max(0, (screen_h - 800) // 2)
        self.root.geometry(f"1100x800+{x}+{y}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config = self._load_config()
        self.personas = self._load_personas()
        self.engine = None
        self.running = False
        self.script_entries = []
        self._current_products = []

        self._build_ui()
        self._apply_config()

    # v1.61：GlobalTaskQueue 被传入 self（KoboApp），但它需要 Tk 的 after() 方法
    # 委派给 self.root，避免 AttributeError
    def after(self, ms, func=None, *args):
        if func is None:
            # Tk after 签名：after(ms) 返回 handle；after(ms, func) 排队
            return self.root.after(ms)
        return self.root.after(ms, func, *args)

    def _load_config(self) -> dict:
        config_file = os.path.join(_app_dir(), CONFIG_FILE)
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def _save_config(self):
        # v1.47：先把现存 defaults 留出来，避免覆盖 trace 方法写入的字段
        # （video_folder / material_folders / industry / insert_strategy 等）
        existing_defaults = dict(self.config.get("defaults", {})) if isinstance(self.config.get("defaults"), dict) else {}
        # v1.62：先保留现有 api 字段，再用 UI 输入覆盖
        # 这样 video_infinite_workflow / 以后新加的 api 字段不会被点保存清掉
        existing_api = dict(self.config.get("api", {})) if isinstance(self.config.get("api"), dict) else {}
        config = {
            "api": existing_api,
            "defaults": existing_defaults,
        }
        config["api"]["key"] = self.api_key_var.get()
        config["api"]["base_url"] = self.base_url_var.get()
        config["api"]["audio_workflow"] = self.audio_wf_var.get()
        config["api"]["video_workflow"] = self.video_wf_var.get()
        # v1.62：modes 也是声明性字段，不能被点保存清掉
        config["modes"] = self.config.get("modes", [])
        config["defaults"]["output_dir"] = self.output_dir_var.get()
        # v1.21：AI 批量执行输出目录（Tab 6 用），与手动混剪分开
        config["defaults"]["ai_output_dir"] = existing_defaults.get(
            "ai_output_dir", "/Users/zxz/Desktop/口播成品"
        )
        config["max_concurrent"] = self.max_concurrent_var.get()
        config["action_prompts"] = self.config.get("action_prompts", [])
        # v2.10.26: 覆盖率相关 4 个 var（mat_pct / coverage_mode / enable_coverage / second_review）
        # 由 ai_tab 自己 trace 调 _save_config 写盘; 这里兜底写一份（避免 trace 漏掉时 yaml 缺字段）
        config["defaults"]["mat_pct"] = existing_defaults.get("mat_pct", 50.0)
        config["defaults"]["coverage_mode"] = existing_defaults.get("coverage_mode", "free")
        config["defaults"]["enable_coverage"] = existing_defaults.get("enable_coverage", True)
        config["defaults"]["second_review"] = existing_defaults.get("second_review", True)
        # v2.10.40 新增: extract_mode 持久化（之前漏了，重启软件会回默认）
        config["defaults"]["extract_mode"] = existing_defaults.get("extract_mode", "🎲 随机")
        config["asr"] = {
            "url": self.asr_url_var.get().strip() or ASR_DEFAULT_URL,
            "key": self.asr_key_var.get().strip(),
            "model": self.asr_model_var.get().strip() or ASR_DEFAULT_MODEL,
        }
        config["llm"] = {
            "url": self.llm_url_var.get().strip() or LLM_DEFAULT_URL,
            "key": self.llm_key_var.get().strip(),
            "model": self.llm_model_var.get().strip() or LLM_DEFAULT_MODEL,
        }
        save_path = os.path.join(_app_dir(), CONFIG_FILE)
        with open(save_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        # v1.47：同步回 self.config，让后续 trace/callers 都能读到完整 defaults
        self.config["defaults"] = config["defaults"]

    # ========== v2.10.57 新增：一键清空所有配置（Win 上"残留"场景专用） ==========
    def _clear_all_config(self):
        """清空所有用户敏感配置 + 路径记忆：
        - API Key / Base URL / Workflow ID
        - ASR (URL/Key/Model) - 重置回默认
        - LLM (URL/Key/Model) - 重置回默认
        - 人设列表（personas.json）
        - Tab 6 主视频文件夹 / 素材文件夹列表
        - output_dir / ai_output_dir

        不清：modes / action_prompts / max_concurrent（这些是配置习惯不是数据）
        """
        if not messagebox.askyesno(
            "⚠️ 清空所有配置",
            "将清空以下内容（清空后需重启软件生效）：\n\n"
            "• API Key + Base URL + Workflow ID\n"
            "• ASR / LLM 的 Key（URL 和 Model 还原默认）\n"
            "• 人设列表（含图片/视频/声音样本）\n"
            "• Tab 6 主视频文件夹 + 素材文件夹列表\n"
            "• 成品输出目录\n\n"
            "确定要继续吗？",
            icon="warning",
        ):
            return
        # 二次确认（危险操作）
        if not messagebox.askyesno(
            "再次确认",
            "⚠️ 此操作不可恢复！\n\n人设会被永久删除（不会进入回收站）。\n\n真的清空吗？",
            icon="warning",
        ):
            return

        cleared = []

        # 1. 清 API
        self.api_key_var.set("")
        self.config.setdefault("api", {})
        self.config["api"]["key"] = ""
        cleared.append("API Key")

        # 2. 重置 ASR（保留默认 URL/Model，只清 key）
        self.asr_key_var.set("")
        cleared.append("ASR Key")

        # 3. 重置 LLM（保留默认 URL/Model，只清 key）
        self.llm_key_var.set("")
        cleared.append("LLM Key")

        # 4. 清人设
        try:
            self.personas = {}
            self._save_personas()
            cleared.append("人设列表")
        except Exception as e:
            self._log(f"⚠️ 清人设失败: {e}")

        # 5. 清 Tab 6 主视频文件夹 + 素材文件夹
        try:
            self.video_folder_var.set("")
            if hasattr(self, "ai_tab") and self.ai_tab is not None:
                self.ai_tab.material_folders.clear()
                if hasattr(self.ai_tab, "_save_material_folders"):
                    self.ai_tab._save_material_folders()
                if hasattr(self.ai_tab, "_refresh_material_list"):
                    self.ai_tab._refresh_material_list()
            self.config.setdefault("defaults", {})
            self.config["defaults"]["video_folder"] = ""
            self.config["defaults"]["material_folders"] = []
            cleared.append("Tab 6 文件夹")
        except Exception as e:
            self._log(f"⚠️ 清 Tab 6 文件夹失败: {e}")

        # 6. 清 output_dir
        try:
            self.output_dir_var.set(str(Path.home() / "Desktop" / "口播成品"))
            self.config["defaults"]["output_dir"] = str(Path.home() / "Desktop" / "口播成品")
            cleared.append("输出目录")
        except Exception as e:
            self._log(f"⚠️ 清输出目录失败: {e}")

        # 7. 写 config.yaml
        try:
            self._save_config()
        except Exception as e:
            self._log(f"⚠️ 写 config.yaml 失败: {e}")

        # 8. 刷新人设 UI
        try:
            self._refresh_persona_combos()
        except Exception as e:
            self._log(f"⚠️ 刷新人设 UI 失败: {e}")

        self._log(f"🧹 已清空: {', '.join(cleared)}")
        messagebox.showinfo(
            "完成",
            f"已清空：{', '.join(cleared)}\n\n建议重启软件确保 UI 完全同步。",
        )

    # ========== v1.12: ASR / LLM 测试连接与 Key 切换 ==========
    def _toggle_key_visibility(self, entry_widget):
        """切换 Key 输入框的显示/隐藏"""
        try:
            current_show = entry_widget.cget("show")
        except Exception:
            current_show = ""
        entry_widget.config(show="" if current_show else "•")

    def _test_asr_connection(self):
        url = self.asr_url_var.get().strip() or ASR_DEFAULT_URL
        key = self.asr_key_var.get().strip()
        model = self.asr_model_var.get().strip() or ASR_DEFAULT_MODEL
        if not key:
            messagebox.showwarning("提示", "请先填 ASR API Key")
            return

        def do_test():
            try:
                # ASR 接口要传文件，先用一个 0.1s 静音 wav 探活
                import urllib.request, urllib.error
                # 试探：拉模型列表 / 或带空 payload POST 都行，这里直接 POST 一个最小请求
                probe_url = url.replace("/audio/transcriptions", "/models")
                req = urllib.request.Request(probe_url, headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    code = resp.getcode()
                    body = resp.read().decode("utf-8", errors="ignore")[:200]
                    self.root.after(0, lambda: messagebox.showinfo(
                        "ASR 连接成功",
                        f"✅ 已连通 {url}\n模型：{model}\nHTTP {code}\n（前 200 字：{body}…）"
                    ))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror(
                    "ASR 连接失败",
                    f"❌ {err}\n\n排查：\n1) Key 是否正确\n2) URL 是否能访问\n3) 网络是否能通 {url.split('/v1')[0]}"
                ))

        threading.Thread(target=do_test, daemon=True).start()

    def _test_llm_connection(self):
        url = self.llm_url_var.get().strip() or LLM_DEFAULT_URL
        key = self.llm_key_var.get().strip()
        model = self.llm_model_var.get().strip() or LLM_DEFAULT_MODEL
        if not key:
            messagebox.showwarning("提示", "请先填 LLM API Key")
            return

        def do_test():
            try:
                import urllib.request, urllib.error, json as _json
                payload = _json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "temperature": LLM_TEMPERATURE,
                    "max_tokens": 32,
                }).encode("utf-8")
                req = urllib.request.Request(
                    url, data=payload, method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {key}",
                    },
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    code = resp.getcode()
                    body = resp.read().decode("utf-8", errors="ignore")[:200]
                    self.root.after(0, lambda: messagebox.showinfo(
                        "LLM 连接成功",
                        f"✅ 已连通 {url}\n模型：{model}（温度 {LLM_TEMPERATURE} / 长度 {LLM_MAX_TOKENS}）\nHTTP {code}\n（前 200 字：{body}…）"
                    ))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror(
                    "LLM 连接失败",
                    f"❌ {err}\n\n排查：\n1) Key 是否正确\n2) URL 是否能访问\n3) 模型名是否拼对（区分大小写）"
                ))

        threading.Thread(target=do_test, daemon=True).start()

    def _load_personas(self) -> dict:
        p = os.path.join(_app_dir(), PERSONAS_FILE)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_personas(self):
        p = os.path.join(_app_dir(), PERSONAS_FILE)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.personas, f, ensure_ascii=False, indent=2)

    def _apply_config(self):
        api = self.config.get("api", {})
        defaults = self.config.get("defaults", {})
        self.api_key_var.set(api.get("key", ""))
        self.base_url_var.set(api.get("base_url", "https://www.runninghub.cn/openapi/v2"))
        self.audio_wf_var.set(api.get("audio_workflow", "1932357354590040066"))
        self.video_wf_var.set(api.get("video_workflow", "1981022739760812034"))
        self.output_dir_var.set(defaults.get("output_dir", str(Path.home() / "Desktop" / "口播成品")))
        # v1.44：成品 Tab 改 output_dir 时同步给 tab5 / 持久化
        self.output_dir_var.trace_add("write", self._on_output_dir_changed)
        self.max_concurrent_var.set(str(self.config.get("max_concurrent", "5")))

        # v1.12: ASR / LLM 配置读取
        asr_cfg = self.config.get("asr", {})
        self.asr_url_var.set(asr_cfg.get("url", ASR_DEFAULT_URL))
        self.asr_key_var.set(asr_cfg.get("key", ""))
        self.asr_model_var.set(asr_cfg.get("model", ASR_DEFAULT_MODEL))
        llm_cfg = self.config.get("llm", {})
        self.llm_url_var.set(llm_cfg.get("url", LLM_DEFAULT_URL))
        self.llm_key_var.set(llm_cfg.get("key", ""))
        self.llm_model_var.set(llm_cfg.get("model", LLM_DEFAULT_MODEL))

        # v2.10.26: 持久化的覆盖率配置（启动恢复，由 AITab init 时读 self.app.config.setdefault("defaults", {}).get(...) 复用）
        # 这里把 yaml 里的 4 个 var 写到 defaults（AITab 后续读 defaults 恢复）
        # 注意：不能直接 set AITab 的 tk var，因为 AITab 还没创建。改为写回 self.config["defaults"]
        # AITab init 时读取 self.app.config 取值并 set 到自己的 tk var
        # 这里不需要写，等 AITab init 自动从 self.config.get("defaults", {}) 读

        self._refresh_persona_combos()

    def _get_persona_random_info(self, name):
        if name not in self.personas:
            return "未选择", "未选择"
        p = self.personas[name]
        checked_imgs = [i for i in p.get("images", []) if i.get("checked")]
        checked_auds = [a for a in p.get("audios", []) if a.get("checked")]
        img_info = f"固定: {os.path.basename(checked_imgs[0]['path'])}" if len(checked_imgs) == 1 else f"随机({len(checked_imgs)}张)"
        aud_info = f"固定: {os.path.basename(checked_auds[0]['path'])}" if len(checked_auds) == 1 else f"随机({len(checked_auds)}个)"
        return img_info, aud_info

    def _pick_persona_files(self, name):
        p = self.personas.get(name, {})
        checked_imgs = [i["path"] for i in p.get("images", []) if i.get("checked")]
        checked_auds = [a["path"] for a in p.get("audios", []) if a.get("checked")]
        if not checked_imgs or not checked_auds:
            return None, None
        return random.choice(checked_imgs), random.choice(checked_auds)

    def _pick_persona_heygem_files(self, name):
        """v1.67：Heygem 模式取形象视频 + 样本声音（不取图片）"""
        p = self.personas.get(name, {})
        checked_vids = [v["path"] for v in p.get("videos", []) if v.get("checked")]
        checked_auds = [a["path"] for a in p.get("audios", []) if a.get("checked")]
        if not checked_vids or not checked_auds:
            return None, None
        return random.choice(checked_vids), random.choice(checked_auds)

    def _calc_script_height(self, count):
        heights = {1: 35, 2: 15, 3: 9, 4: 6}
        return heights.get(count, 4)

    def _build_ui(self):
        self.api_key_var = tk.StringVar()
        self.base_url_var = tk.StringVar()
        self.audio_wf_var = tk.StringVar()
        self.video_wf_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.max_concurrent_var = tk.StringVar(value="5")
        # v1.51：生成模式选择（从 yaml 读 modes 列表，存选中项的 name）
        self.mode_var = tk.StringVar()

        # v1.12: ASR / LLM 配置变量
        self.asr_url_var = tk.StringVar(value=ASR_DEFAULT_URL)
        self.asr_key_var = tk.StringVar()
        self.asr_model_var = tk.StringVar(value=ASR_DEFAULT_MODEL)
        self.llm_url_var = tk.StringVar(value=LLM_DEFAULT_URL)
        self.llm_key_var = tk.StringVar()
        self.llm_model_var = tk.StringVar(value=LLM_DEFAULT_MODEL)

        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        task_frame = ttk.Frame(notebook, padding=10)
        notebook.add(task_frame, text="  任务  ")

        persona_frame = ttk.Frame(notebook, padding=10)
        notebook.add(persona_frame, text="  人设管理  ")

        settings_frame = ttk.Frame(notebook, padding=10)
        notebook.add(settings_frame, text="  设置  ")

        product_frame = ttk.Frame(notebook, padding=10)
        notebook.add(product_frame, text="  成品  ")

        merge_frame = ttk.Frame(notebook, padding=10)
        notebook.add(merge_frame, text="  混剪/插段  ")

        # v1.13: Tab 6「🤖 ai智能」
        ai_frame = ttk.Frame(notebook, padding=10)
        notebook.add(ai_frame, text="  🤖 ai智能  ")

        self._build_task_tab(task_frame)
        self._build_persona_tab(persona_frame)
        self._build_settings_tab(settings_frame)
        self._build_product_tab(product_frame)
        self.merge_tab = build_merge_tab(merge_frame, self)
        self.ai_tab = build_ai_tab(ai_frame, self)

        self.notebook = notebook
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # v1.44 全局任务队列
        self.task_queue = GlobalTaskQueue(self)
        self._build_global_status_bar()

        # 默认切到第 5 个 Tab「混剪/插段」（v1.5 新增）
        try:
            notebook.select(4)
        except Exception:
            pass

    def _build_task_tab(self, parent):
        row = 0

        header_frame = ttk.Frame(parent)
        header_frame.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=5)

        ttk.Label(header_frame, text=f"任务（单次最高{MAX_BATCH}条）", font=("", 10, "bold")).pack(side=tk.LEFT)

        self.add_btn = ttk.Button(header_frame, text="➕ 添加文案框", command=self._add_script_box)
        self.add_btn.pack(side=tk.LEFT, padx=15)

        ttk.Button(header_frame, text="📥 批量导入", command=self._import_scripts).pack(side=tk.LEFT, padx=5)

        # v1.98：统一人设按钮 - 把所有文案框的 persona 设成第一个文案框的 persona
        ttk.Button(header_frame, text="🎭 统一人设", command=self._uniform_personas).pack(side=tk.LEFT, padx=5)

        # v1.54：删除 Tab 顶部全局"⚙️ 管理提示词"按钮，改到每个文案框 prompt_combo 旁边

        self.count_label = ttk.Label(header_frame, text=f"1/{MAX_BATCH}")
        self.count_label.pack(side=tk.LEFT, padx=5)

        row += 1

        ttk.Label(parent, text="输出目录:").grid(row=row, column=0, sticky=tk.W, pady=5)
        out_frame = ttk.Frame(parent)
        out_frame.grid(row=row, column=1, sticky=tk.EW, pady=5, padx=5)
        ttk.Entry(out_frame, textvariable=self.output_dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(out_frame, text="浏览", command=lambda: self._browse_dir(self.output_dir_var)).pack(side=tk.RIGHT, padx=(5, 0))
        row += 1

        # v1.51：生成模式下拉框（替换原来的"仅生成语音"复选框）
        mode_frame = ttk.Frame(parent)
        mode_frame.grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(mode_frame, text="生成模式:").pack(side=tk.LEFT, padx=(0, 8))
        # 从 yaml 读 modes 列表填下拉框
        modes_cfg = (self.config or {}).get("modes", []) or []
        mode_names = [m.get("name", m.get("id", "")) for m in modes_cfg]
        if not mode_names:
            # 兼容旧配置没写 modes 的情况，给两个默认项
            mode_names = ["🎙 仅生成语音", "🎬 语音+视频"]
        self.mode_combo = ttk.Combobox(mode_frame, textvariable=self.mode_var, state="readonly", width=24)
        self.mode_combo["values"] = mode_names
        self.mode_combo.pack(side=tk.LEFT)
        # 默认值：找 flow=='video' 的 mode，否则用列表第二项（保持原"勾选默认=全视频"行为），最后兜底取第一项
        default_mode_name = None
        for m in modes_cfg:
            if m.get("flow") == "video":
                default_mode_name = m.get("name")
                break
        if not default_mode_name:
            default_mode_name = mode_names[1] if len(mode_names) > 1 else mode_names[0]
        self.mode_var.set(default_mode_name)
        # v1.52：mode 切换时按 flow 显隐字段
        self.mode_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_mode_visibility())

        # v1.66：Heygem 数字人模式固定走 select=2（参考音+文案），不暴露开关 UI
        row += 1

        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=5)
        row += 1

        scripts_container = ttk.Frame(parent)
        scripts_container.grid(row=row, column=0, columnspan=2, sticky=tk.NSEW, pady=5)

        self.scripts_canvas = tk.Canvas(scripts_container, highlightthickness=0)
        self.scripts_scrollbar = ttk.Scrollbar(scripts_container, orient=tk.VERTICAL, command=self.scripts_canvas.yview)
        self.scripts_scrollable = ttk.Frame(self.scripts_canvas)

        self.scripts_scrollable.bind("<Configure>", lambda e: self.scripts_canvas.configure(scrollregion=self.scripts_canvas.bbox("all")))
        self._scripts_canvas_win = self.scripts_canvas.create_window((0, 0), window=self.scripts_scrollable, anchor="nw")
        self.scripts_canvas.configure(yscrollcommand=self.scripts_scrollbar.set)
        self.scripts_canvas.bind("<Configure>", lambda e: self.scripts_canvas.itemconfig(self._scripts_canvas_win, width=e.width))

        self.scripts_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scripts_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.scripts_canvas.bind("<Enter>", self._bind_mousewheel)
        self.scripts_canvas.bind("<Leave>", self._unbind_mousewheel)

        row += 1

        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(row - 1, weight=1)

        self._add_script_box()

        action_frame = ttk.Frame(parent)
        action_frame.grid(row=row + 1, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))

        self.run_btn = ttk.Button(action_frame, text="🚀 开始生成", command=self._on_run)
        self.run_btn.pack(side=tk.LEFT, padx=5, ipadx=20, ipady=5)

        self.stop_btn = ttk.Button(action_frame, text="⏹ 停止", command=self._on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5, ipadx=10, ipady=5)

    def _bind_mousewheel(self, event):
        self.scripts_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, event):
        self.scripts_canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self.scripts_canvas.yview_scroll(direction, "units")

    def _build_persona_tab(self, parent):
        row = 0

        ttk.Label(parent, text="已保存人设:", font=("", 10, "bold")).grid(row=row, column=0, sticky=tk.W, pady=5)
        row += 1

        list_frame = ttk.Frame(parent)
        list_frame.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW, pady=5)

        self.persona_canvas = tk.Canvas(list_frame, height=200, highlightthickness=0, bg="white")
        persona_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.persona_canvas.yview)
        self.persona_canvas.configure(yscrollcommand=persona_scroll.set)
        self.persona_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        persona_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.persona_canvas.bind("<Button-1>", self._on_persona_canvas_click)
        self.persona_canvas.bind("<Configure>", lambda e: self._draw_persona_list())
        self._persona_selected_idx = -1
        self._persona_thumb_refs = []
        self._persona_row_rects = []
        row += 1

        btn_frame = ttk.Frame(parent)
        btn_frame.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=5)
        ttk.Button(btn_frame, text="➕ 新增人设", command=self._add_persona).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="✏️ 编辑选中", command=self._edit_persona).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🗑 删除选中", command=self._delete_persona).pack(side=tk.LEFT, padx=5)
        row += 1

        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=10)
        row += 1

        detail_frame = ttk.LabelFrame(parent, text="人设详情", padding=10)
        detail_frame.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW, pady=5)

        detail_container = ttk.Frame(detail_frame)
        detail_container.pack(fill=tk.BOTH, expand=True)

        self.persona_detail_canvas = tk.Canvas(detail_container, highlightthickness=0, bg="#f5f5f5")
        detail_scroll = ttk.Scrollbar(detail_container, orient=tk.VERTICAL, command=self.persona_detail_canvas.yview)
        self.persona_detail_canvas.configure(yscrollcommand=detail_scroll.set)
        self.persona_detail_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        detail_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.persona_detail_inner = ttk.Frame(self.persona_detail_canvas)
        self._detail_win = self.persona_detail_canvas.create_window((0, 0), window=self.persona_detail_inner, anchor="nw")
        self.persona_detail_inner.bind("<Configure>", lambda e: self.persona_detail_canvas.configure(scrollregion=self.persona_detail_canvas.bbox("all")))
        self.persona_detail_canvas.bind("<Configure>", lambda e: self.persona_detail_canvas.itemconfig(self._detail_win, width=e.width))

        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(row, weight=1)

    def _build_settings_tab(self, parent):
        # v1.12: 包滚动条（ASR/LLM 后高度爆了，加滚动才能看到保存按钮）
        _container = ttk.Frame(parent)
        _container.pack(fill=tk.BOTH, expand=True)

        _canvas = tk.Canvas(_container, highlightthickness=0)
        _scroll = ttk.Scrollbar(_container, orient=tk.VERTICAL, command=_canvas.yview)
        _canvas.configure(yscrollcommand=_scroll.set)
        _scroll.pack(side=tk.RIGHT, fill=tk.Y)
        _canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        _inner = ttk.Frame(_canvas)
        _win_id = _canvas.create_window((0, 0), window=_inner, anchor="nw")
        _inner.bind("<Configure>", lambda e: _canvas.configure(scrollregion=_canvas.bbox("all")))
        _canvas.bind("<Configure>", lambda e: _canvas.itemconfig(_win_id, width=e.width))

        def _on_wheel(e):
            if e.delta:
                _canvas.yview_scroll(int(-e.delta), "units")
        _canvas.bind("<Enter>", lambda e: _canvas.bind_all("<MouseWheel>", _on_wheel))
        _canvas.bind("<Leave>", lambda e: _canvas.unbind_all("<MouseWheel>"))

        parent = _inner  # 重新指向滚动容器内的 frame，后续所有 grid 都进这里
        row = 0

        ttk.Label(parent, text="API Key:").grid(row=row, column=0, sticky=tk.W, pady=5)
        ttk.Entry(parent, textvariable=self.api_key_var, width=50, show="*").grid(row=row, column=1, sticky=tk.EW, pady=5, padx=5)
        row += 1

        ttk.Label(parent, text="并发数:").grid(row=row, column=0, sticky=tk.W, pady=5)
        concurrent_frame = ttk.Frame(parent)
        concurrent_frame.grid(row=row, column=1, sticky=tk.W, pady=5, padx=5)
        ttk.Combobox(concurrent_frame, textvariable=self.max_concurrent_var, values=["1", "2", "3", "4", "5"], width=5, state="readonly").pack(side=tk.LEFT)
        ttk.Label(concurrent_frame, text="  1=最稳但慢，5=最快但占满所有槽位").pack(side=tk.LEFT)
        row += 1

        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=15)
        row += 1

        info_text = (
            "使用说明:\n"
            "1. 在「设置」页填入 API Key\n"
            "2. 在「人设管理」页新增人设，录入图片和声音\n"
            "3. 在「任务」页添加文案框、选择人设，点击生成\n"
            "4. 每条文案独立选择人设，独立随机图片和声音\n\n"
            "v1.1.0 功能:\n"
            "- 4并发任务池：音频优先占槽，视频自动补上\n"
            "- 多人设管理：录入多图多声，勾选随机\n"
            "- 每条文案独立选择人设\n"
            "- 动态文案框，最多40条\n"
            "- 文案超60秒自动拆分"
        )
        ttk.Label(parent, text=info_text, justify=tk.LEFT, font=("Microsoft YaHei", 9)).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=5)
        row += 1

        # ===== v1.12: ASR 配置区 =====
        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=10)
        row += 1
        ttk.Label(parent, text="🎙 ASR 语音识别（v1.12）", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(5, 5))
        row += 1

        ttk.Label(parent, text="平台 URL:").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Entry(parent, textvariable=self.asr_url_var, width=50).grid(row=row, column=1, sticky=tk.EW, pady=3, padx=5)
        row += 1

        ttk.Label(parent, text="API Key:").grid(row=row, column=0, sticky=tk.W, pady=3)
        asr_key_frame = ttk.Frame(parent)
        asr_key_frame.grid(row=row, column=1, sticky=tk.EW, pady=3, padx=5)
        self._asr_key_entry = ttk.Entry(asr_key_frame, textvariable=self.asr_key_var, width=42, show="•")
        self._asr_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(asr_key_frame, text="👁", width=3, command=lambda: self._toggle_key_visibility(self._asr_key_entry)).pack(side=tk.LEFT, padx=2)
        row += 1

        ttk.Label(parent, text="模型名:").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Entry(parent, textvariable=self.asr_model_var, width=50).grid(row=row, column=1, sticky=tk.EW, pady=3, padx=5)
        row += 1

        ttk.Button(parent, text="🔌 测试 ASR 连接", command=self._test_asr_connection).grid(row=row, column=1, sticky=tk.W, pady=5, padx=5)
        row += 1

        # ===== v1.12: LLM 配置区 =====
        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=10)
        row += 1
        ttk.Label(parent, text="🤖 LLM 大模型（v1.12）", font=("", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(5, 5))
        row += 1

        ttk.Label(parent, text="平台 URL:").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Entry(parent, textvariable=self.llm_url_var, width=50).grid(row=row, column=1, sticky=tk.EW, pady=3, padx=5)
        row += 1

        ttk.Label(parent, text="API Key:").grid(row=row, column=0, sticky=tk.W, pady=3)
        llm_key_frame = ttk.Frame(parent)
        llm_key_frame.grid(row=row, column=1, sticky=tk.EW, pady=3, padx=5)
        self._llm_key_entry = ttk.Entry(llm_key_frame, textvariable=self.llm_key_var, width=42, show="•")
        self._llm_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(llm_key_frame, text="👁", width=3, command=lambda: self._toggle_key_visibility(self._llm_key_entry)).pack(side=tk.LEFT, padx=2)
        row += 1

        ttk.Label(parent, text="模型名:").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Entry(parent, textvariable=self.llm_model_var, width=50).grid(row=row, column=1, sticky=tk.EW, pady=3, padx=5)
        row += 1

        ttk.Label(parent, text=f"(温度 {LLM_TEMPERATURE} / 长度 {LLM_MAX_TOKENS} 已写死，UI 不暴露)", foreground="#888", font=("", 8)).grid(row=row, column=1, sticky=tk.W, pady=1, padx=5)
        row += 1

        ttk.Button(parent, text="🔌 测试 LLM 连接", command=self._test_llm_connection).grid(row=row, column=1, sticky=tk.W, pady=5, padx=5)
        row += 1

        # 原保存按钮位置
        ttk.Button(parent, text="保存设置", command=self._save_config).grid(row=row, column=0, columnspan=2, pady=15)
        row += 1

        # v2.10.57 新增：一键清空所有配置（Win 上残留人设/路径专用）
        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=(20, 10))
        row += 1
        ttk.Label(parent, text="⚠️ 危险操作区", font=("", 10, "bold"), foreground="#d9534f").grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 5))
        row += 1
        ttk.Button(parent, text="🧹 一键清空所有配置", command=self._clear_all_config).grid(row=row, column=0, columnspan=2, pady=5)

        parent.columnconfigure(1, weight=1)

    def _build_product_tab(self, parent):
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(header, text="输出目录:", font=("", 9)).pack(side=tk.LEFT)
        self.product_dir_label = ttk.Label(header, text="", font=("", 9), foreground="#4a90d9")
        self.product_dir_label.pack(side=tk.LEFT, padx=5)

        ttk.Button(header, text="📂 打开文件夹", command=self._open_output_dir).pack(side=tk.RIGHT, padx=5)

        self.product_count_label = ttk.Label(header, text="", font=("", 9), foreground="#888")
        self.product_count_label.pack(side=tk.RIGHT, padx=10)

        container = ttk.Frame(parent)
        container.pack(fill=tk.BOTH, expand=True)

        self.product_canvas = tk.Canvas(container, highlightthickness=0, bg="#f5f5f5", height=200)
        product_scroll = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self.product_canvas.yview)
        self.product_canvas.configure(yscrollcommand=product_scroll.set)
        self.product_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        product_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.product_list_frame = ttk.Frame(self.product_canvas)
        self._product_canvas_win = self.product_canvas.create_window((0, 0), window=self.product_list_frame, anchor="nw")
        self.product_list_frame.bind("<Configure>", lambda e: self.product_canvas.configure(scrollregion=self.product_canvas.bbox("all")))
        self.product_canvas.bind("<Configure>", lambda e: self.product_canvas.itemconfig(self._product_canvas_win, width=e.width))

        self._product_thumb_refs = []

        progress_frame = ttk.Frame(parent)
        progress_frame.pack(fill=tk.X, pady=(5, 0))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        self.pct_label = ttk.Label(progress_frame, text="0%", width=5, font=("", 9, "bold"))
        self.pct_label.pack(side=tk.LEFT, padx=(0, 5))

        self.status_label = ttk.Label(progress_frame, text="就绪", font=("", 9))
        self.status_label.pack(side=tk.RIGHT)

        log_frame = ttk.LabelFrame(parent, text="运行日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _on_tab_changed(self, event=None):
        try:
            idx = self.notebook.index(self.notebook.select())
            if idx == 3:
                self._refresh_product_list()
        except Exception:
            pass

    def _refresh_product_list(self):
        output_dir = self.output_dir_var.get().strip()
        self.product_dir_label.config(text=output_dir or "未设置")

        for w in self.product_list_frame.winfo_children():
            w.destroy()
        self._product_thumb_refs = []

        if not output_dir:
            ttk.Label(self.product_list_frame, text="输出目录未设置\n请在任务页设置输出目录", font=("Microsoft YaHei", 10), foreground="#999", justify=tk.CENTER).pack(pady=40)
            self.product_count_label.config(text="")
            return

        if not self._current_products:
            ttk.Label(self.product_list_frame, text="暂无成品视频\n执行生成任务后将在此显示", font=("Microsoft YaHei", 10), foreground="#999", justify=tk.CENTER).pack(pady=40)
            self.product_count_label.config(text="")
            return

        self.product_count_label.config(text=f"本轮生成 {len(self._current_products)} 个视频")

        for vf_path in self._current_products:
            if not os.path.exists(vf_path):
                continue

            try:
                stat = os.stat(vf_path)
            except Exception:
                continue

            f = os.path.basename(vf_path)
            rel = os.path.relpath(vf_path, output_dir)
            persona = rel.split(os.sep)[0] if os.sep in rel else ""

            row = ttk.Frame(self.product_list_frame, relief=tk.GROOVE, borderwidth=1, padding=5)
            row.pack(fill=tk.X, pady=2, padx=2)

            thumb = None
            if HAS_PIL:
                try:
                    import subprocess as sp
                    tmp_thumb = os.path.join(tempfile.gettempdir(), f"_kobo_thumb_{vf_path.replace(os.sep, '_').replace('/', '_').replace('\\', '_').replace(':', '')}.png")
                    if not os.path.exists(tmp_thumb) or (time.time() - os.path.getmtime(tmp_thumb)) > 3600:
                        sp.run([
                            _get_ffmpeg_path(), "-y", "-i", vf_path,
                            "-ss", "1", "-vframes", "1",
                            "-vf", "scale=80:-1",
                            tmp_thumb
                        ], capture_output=True, timeout=10)
                    if os.path.exists(tmp_thumb):
                        img = Image.open(tmp_thumb)
                        img.thumbnail((80, 50))
                        thumb = ImageTk.PhotoImage(img)
                        self._product_thumb_refs.append(thumb)
                    else:
                        self._product_thumb_refs.append(None)
                except Exception:
                    self._product_thumb_refs.append(None)
            else:
                self._product_thumb_refs.append(None)

            if thumb:
                ttk.Label(row, image=thumb).pack(side=tk.LEFT, padx=(0, 8))
            else:
                placeholder = ttk.Frame(row, width=80, height=50, relief=tk.SUNKEN, borderwidth=1)
                placeholder.pack(side=tk.LEFT, padx=(0, 8))
                placeholder.pack_propagate(False)
                ttk.Label(placeholder, text="🎬", font=("", 14)).pack(expand=True)

            info_frame = ttk.Frame(row)
            info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

            ttk.Label(info_frame, text=f, font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W)

            detail_parts = []
            if persona:
                detail_parts.append(f"人设: {persona}")
            size_mb = stat.st_size / (1024 * 1024)
            detail_parts.append(f"{size_mb:.1f}MB")
            mtime_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            detail_parts.append(mtime_str)
            ttk.Label(info_frame, text="  |  ".join(detail_parts), font=("Microsoft YaHei", 8), foreground="#888").pack(anchor=tk.W)

            btn_frame = ttk.Frame(row)
            btn_frame.pack(side=tk.RIGHT, padx=5)

            ttk.Button(btn_frame, text="▶", width=3, command=lambda p=vf_path: self._play_video(p)).pack(side=tk.LEFT, padx=2)
            ttk.Button(btn_frame, text="📂", width=3, command=lambda p=vf_path: self._open_video_location(p)).pack(side=tk.LEFT, padx=2)
            ttk.Button(btn_frame, text="🗑", width=3, command=lambda p=vf_path, n=f: self._delete_video(p, n)).pack(side=tk.LEFT, padx=2)

    def _play_video(self, path):
        if not os.path.exists(path):
            messagebox.showerror("错误", "文件不存在")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("错误", f"无法播放: {e}")

    def _open_video_location(self, path):
        if not os.path.exists(path):
            messagebox.showerror("错误", "文件不存在")
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            messagebox.showerror("错误", f"无法打开: {e}")

    def _delete_video(self, path, name):
        if not os.path.exists(path):
            messagebox.showerror("错误", "文件不存在")
            return
        if messagebox.askyesno("确认删除", f"确定删除「{name}」？\n\n此操作不可恢复"):
            try:
                os.remove(path)
                self._log(f"已删除: {name}")
                self._refresh_product_list()
            except Exception as e:
                messagebox.showerror("错误", f"删除失败: {e}")

    def _refresh_persona_combos(self):
        names = list(self.personas.keys())
        self._persona_selected_idx = -1
        self._draw_persona_list()
        for entry in self.script_entries:
            entry["persona_combo"]["values"] = names

    def _draw_persona_list(self):
        c = self.persona_canvas
        c.delete("all")
        self._persona_thumb_refs = []
        self._persona_row_rects = []

        names = list(self.personas.keys())
        if not names:
            c.configure(scrollregion=(0, 0, 0, 0))
            return

        row_h = 50
        pad = 5
        thumb_size = 40
        canvas_w = c.winfo_width() or 600

        for i, name in enumerate(names):
            y = i * (row_h + pad)

            rect = c.create_rectangle(2, y, canvas_w, y + row_h, fill="", outline="", tags=f"row_{i}")
            self._persona_row_rects.append(rect)

            if i == self._persona_selected_idx:
                c.itemconfig(rect, fill="#e0ecff", outline="#4a90d9")

            p = self.personas[name]
            checked_imgs = [img for img in p.get("images", []) if img.get("checked")]
            thumb = None
            if HAS_PIL and checked_imgs:
                try:
                    img = Image.open(checked_imgs[0]["path"])
                    img.thumbnail((thumb_size, thumb_size))
                    thumb = ImageTk.PhotoImage(img)
                    self._persona_thumb_refs.append(thumb)
                except Exception:
                    self._persona_thumb_refs.append(None)
            else:
                self._persona_thumb_refs.append(None)

            if thumb:
                c.create_image(pad + 2, y + (row_h - thumb_size) // 2, anchor=tk.NW, image=thumb)
            else:
                c.create_rectangle(pad + 2, y + (row_h - thumb_size) // 2, pad + 2 + thumb_size, y + (row_h - thumb_size) // 2 + thumb_size, fill="#ddd", outline="#bbb")
                c.create_text(pad + 2 + thumb_size // 2, y + row_h // 2, text="无图", fill="#999", font=("Microsoft YaHei", 8))

            text_x = pad + thumb_size + 12
            c.create_text(text_x, y + 14, anchor=tk.NW, text=name, font=("Microsoft YaHei", 11, "bold"), fill="#333")

            img_count = len(p.get("images", []))
            vid_count = len(p.get("videos", []))
            aud_count = len(p.get("audios", []))
            info_text = f"📷 {img_count}张  🎬 {vid_count}个  🎤 {aud_count}个"
            c.create_text(text_x, y + 34, anchor=tk.NW, text=info_text, font=("Microsoft YaHei", 9), fill="#888")

        total_h = len(names) * (row_h + pad)
        c.configure(scrollregion=(0, 0, canvas_w, total_h))

    def _on_persona_canvas_click(self, event):
        c = self.persona_canvas
        cy = c.canvasy(event.y)
        row_h = 50
        pad = 5
        names = list(self.personas.keys())
        clicked = -1
        for i in range(len(names)):
            y = i * (row_h + pad)
            if y <= cy <= y + row_h:
                clicked = i
                break

        if clicked >= 0:
            self._persona_selected_idx = clicked
            self._draw_persona_list()
            self._on_persona_select()

    def _get_selected_persona_name(self):
        names = list(self.personas.keys())
        if 0 <= self._persona_selected_idx < len(names):
            return names[self._persona_selected_idx]
        return None

    def _on_script_persona_change(self, entry, event=None):
        name = entry["persona_var"].get()
        img_info, aud_info = self._get_persona_random_info(name)
        entry["img_label"].config(text=f"📷 {img_info}")
        entry["aud_label"].config(text=f"🎤 {aud_info}")

    def _uniform_personas(self):
        """v1.98：把所有文案框的 persona 设成第一个文案框的 persona"""
        if len(self.script_entries) < 2:
            messagebox.showinfo("提示", "至少需要 2 条文案框才能使用统一人设")
            return
        first = self.script_entries[0]
        name = first["persona_var"].get().strip()
        if not name:
            messagebox.showwarning("提示", "第一个文案框还没选人设，请先选")
            return
        count = 0
        for entry in self.script_entries[1:]:
            entry["persona_var"].set(name)
            self._on_script_persona_change(entry)
            count += 1
        self._log(f"🎭 已统一 {count} 条文案框的人设为「{name}」")

    def _on_persona_select(self, event=None):
        name = self._get_selected_persona_name()
        if not name:
            return
        p = self.personas.get(name, {})

        for w in self.persona_detail_inner.winfo_children():
            w.destroy()

        ttk.Label(self.persona_detail_inner, text=f"人设名称: {name}", font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, pady=(0, 5))

        history = self._load_stats_history()
        persona_records = [h for h in history if h.get("persona") == name and h.get("success")]
        p = self.personas.get(name, {})
        if len(persona_records) >= 10:
            total_chars = sum(h["chars"] for h in persona_records)
            total_elapsed = sum(h["elapsed"] for h in persona_records)
            speed = total_elapsed / total_chars if total_chars > 0 else 0
            if speed > 0:
                ttk.Label(self.persona_detail_inner, text=f"历史样本: {speed:.2f}秒/字（基于{len(persona_records)}条）", font=("Microsoft YaHei", 9), foreground="#888").pack(anchor=tk.W, pady=(0, 5))
        else:
            ttk.Label(self.persona_detail_inner, text=f"历史样本: {len(persona_records)}/10条", font=("Microsoft YaHei", 9), foreground="#aaa").pack(anchor=tk.W, pady=(0, 5))

        ttk.Label(self.persona_detail_inner, text="形象图片:", font=("Microsoft YaHei", 9, "bold")).pack(anchor=tk.W, pady=(5, 2))
        for i, img in enumerate(p.get("images", [])):
            self._render_detail_checkbox_row(name, "images", i, img)
        checked_imgs = [i for i in p.get("images", []) if i.get("checked")]
        if len(checked_imgs) == 1:
            ttk.Label(self.persona_detail_inner, text=f"  → 固定: {os.path.basename(checked_imgs[0]['path'])}", font=("Microsoft YaHei", 9), foreground="#4a90d9").pack(anchor=tk.W)
        elif len(checked_imgs) > 1:
            ttk.Label(self.persona_detail_inner, text=f"  → 随机（{len(checked_imgs)}张）", font=("Microsoft YaHei", 9), foreground="#4a90d9").pack(anchor=tk.W)

        ttk.Label(self.persona_detail_inner, text="\n形象视频:", font=("Microsoft YaHei", 9, "bold")).pack(anchor=tk.W, pady=(5, 2))
        vids = p.get("videos", [])
        if not vids:
            ttk.Label(self.persona_detail_inner, text="  （未添加）", font=("Microsoft YaHei", 9), foreground="#aaa").pack(anchor=tk.W)
        for v in vids:
            self._render_detail_checkbox_row(name, "videos", None, v, extra_btn=lambda row, vp=v["path"]: ttk.Button(row, text="▶", width=3, command=lambda p=vp: self._open_video_file(p)).pack(side=tk.LEFT, padx=5))
        checked_vids = [v for v in vids if v.get("checked")]
        if len(checked_vids) == 1:
            ttk.Label(self.persona_detail_inner, text=f"  → 固定: {os.path.basename(checked_vids[0]['path'])}", font=("Microsoft YaHei", 9), foreground="#4a90d9").pack(anchor=tk.W)
        elif len(checked_vids) > 1:
            ttk.Label(self.persona_detail_inner, text=f"  → 随机（{len(checked_vids)}个）", font=("Microsoft YaHei", 9), foreground="#4a90d9").pack(anchor=tk.W)

        ttk.Label(self.persona_detail_inner, text="\n样本声音:", font=("Microsoft YaHei", 9, "bold")).pack(anchor=tk.W, pady=(5, 2))
        for a in p.get("audios", []):
            audio_path = a["path"]
            sample_chars = a.get("chars", 0)
            speed_text = "(默认250字/分)"
            if sample_chars > 0 and os.path.exists(audio_path):
                try:
                    audio_duration = _probe_duration(audio_path)
                    if audio_duration is None:
                        raise RuntimeError("no probe tool")
                    speed = audio_duration / sample_chars
                    speed_text = f"({speed:.2f}秒/字)"
                except Exception:
                    pass
            self._render_detail_checkbox_row(
                name, "audios", None, a,
                extra_btn=lambda row, ap=audio_path, st=speed_text: (
                    ttk.Button(row, text="▶", width=3, command=lambda p=ap: self._play_audio_file(p, None)).pack(side=tk.LEFT, padx=5),
                    ttk.Label(row, text=st, font=("Microsoft YaHei", 8), foreground="#666").pack(side=tk.LEFT, padx=5),
                ),
            )
        checked_auds = [a for a in p.get("audios", []) if a.get("checked")]
        if len(checked_auds) == 1:
            ttk.Label(self.persona_detail_inner, text=f"  → 固定: {os.path.basename(checked_auds[0]['path'])}", font=("Microsoft YaHei", 9), foreground="#4a90d9").pack(anchor=tk.W)
        elif len(checked_auds) > 1:
            ttk.Label(self.persona_detail_inner, text=f"  → 随机（{len(checked_auds)}个）", font=("Microsoft YaHei", 9), foreground="#4a90d9").pack(anchor=tk.W)

    def _render_detail_checkbox_row(self, persona_name, category, item_index, item_dict, extra_btn=None):
        """详情面板里渲染一行：真的 checkbox + 文件名。点 checkbox 立刻保存+刷新。
        category: 'images' / 'videos' / 'audios'（仅用于回调时定位 dict）
        item_index: 在 list 里的位置（videos/audios 传 None）
        item_dict: dict 引用
        extra_btn: 可选回调，传入 row Frame，可加额外按钮
        """
        row = ttk.Frame(self.persona_detail_inner)
        row.pack(fill=tk.X, pady=1, padx=2)

        var = tk.BooleanVar(value=bool(item_dict.get("checked")))
        cat_idx = (category, item_index)

        def _on_toggle():
            item_dict["checked"] = var.get()
            self._save_personas()
            self._draw_persona_list()
            self._on_persona_select()

        cb = ttk.Checkbutton(row, variable=var, command=_on_toggle)
        cb.pack(side=tk.LEFT)

        name = os.path.basename(item_dict["path"])
        ttk.Label(row, text=name, width=30).pack(side=tk.LEFT, padx=5)

        if extra_btn is not None:
            extra_btn(row)

    def _open_video_file(self, path):
        if not os.path.exists(path):
            messagebox.showerror("错误", f"视频文件不存在:\n{path}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("错误", f"无法打开: {e}")

    def _play_audio_file(self, path, btn):
        if not os.path.exists(path):
            messagebox.showerror("错误", f"声音文件不存在:\n{path}")
            return
        try:
            if HAS_PYGAME:
                if pygame.mixer.music.get_busy() and getattr(btn, '_playing_path', None) == path:
                    pygame.mixer.music.stop()
                    btn.configure(text="▶")
                    btn._playing_path = None
                    return
                pygame.mixer.music.stop()
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
                btn.configure(text="⏸")
                btn._playing_path = path
                self.root.after(200, lambda: self._watch_playback_main(btn, path))
                self._log(f"试听: {os.path.basename(path)}")
            else:
                if sys.platform == "win32":
                    os.startfile(path)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", path])
                else:
                    subprocess.Popen(["xdg-open", path])
                self._log(f"试听: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("错误", f"无法播放: {e}")

    def _watch_playback_main(self, btn, path):
        if not pygame.mixer.music.get_busy():
            btn.configure(text="▶")
            btn._playing_path = None
            return
        if getattr(btn, '_playing_path', None) == path:
            self.root.after(200, lambda: self._watch_playback_main(btn, path))
        else:
            btn.configure(text="▶")

    def _add_persona(self):
        dlg = PersonaDialog(self.root)
        if dlg.result:
            name = dlg.result["name"]
            self.personas[name] = dlg.result["data"]
            self._save_personas()
            self._refresh_persona_combos()
            self._log(f"人设已新增: {name}")

    def _edit_persona(self):
        name = self._get_selected_persona_name()
        if not name:
            messagebox.showwarning("提示", "请先选择要编辑的人设")
            return
        dlg = PersonaDialog(self.root, persona_data=self.personas.get(name), persona_name=name)
        if dlg.result:
            new_name = dlg.result["name"]
            if new_name != name:
                del self.personas[name]
            self.personas[new_name] = dlg.result["data"]
            self._save_personas()
            self._refresh_persona_combos()
            self._log(f"人设已更新: {new_name}")

    def _delete_persona(self):
        name = self._get_selected_persona_name()
        # v2.10.57 修复：未选中时如果有现有人设，自动选第一个（避免 Win 上点删除按钮"没反应"）
        if not name and self.personas:
            first_name = next(iter(self.personas.keys()), None)
            if first_name:
                self._persona_selected_idx = 0
                self._draw_persona_list()
                name = first_name
                self._log(f"未选 → 默认选第一个人设: {name}")
        if not name:
            messagebox.showwarning("提示", "请先选择要删除的人设")
            return
        if messagebox.askyesno("确认", f"确定删除人设「{name}」？"):
            del self.personas[name]
            self._save_personas()
            self._refresh_persona_combos()
            # v2.10.57：删除后 idx 重置后选下一个（如果还有），方便连续删
            if self.personas:
                self._persona_selected_idx = 0
                self._draw_persona_list()
            self._log(f"人设已删除: {name}")

    def _open_prompt_manager(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("管理动作提示词")
        dlg.geometry("500x400")
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="预设动作提示词列表（点击选择后编辑）", font=("", 10, "bold")).pack(pady=10)

        list_frame = ttk.Frame(dlg)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        listbox = tk.Listbox(list_frame, font=("", 10))
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox.config(yscrollcommand=scrollbar.set)

        for p in self.config.get("action_prompts", []):
            listbox.insert(tk.END, p)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        def add_prompt():
            listbox.insert(tk.END, "新提示词（双击编辑）")

        def edit_prompt():
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一条提示词", parent=dlg)
                return
            idx = sel[0]
            edit_dlg = tk.Toplevel(dlg)
            edit_dlg.title("编辑提示词")
            edit_dlg.geometry("400x150")
            edit_dlg.transient(dlg)
            edit_dlg.grab_set()

            ttk.Label(edit_dlg, text="提示词内容:").pack(pady=5)
            entry = ttk.Entry(edit_dlg, width=50)
            entry.pack(padx=10)
            entry.insert(0, listbox.get(idx))
            entry.select_range(0, tk.END)
            entry.focus()

            def save_edit():
                val = entry.get().strip()
                if val:
                    listbox.delete(idx)
                    listbox.insert(idx, val)
                edit_dlg.destroy()

            ttk.Button(edit_dlg, text="保存", command=save_edit).pack(pady=10)

        def delete_prompt():
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一条提示词", parent=dlg)
                return
            if messagebox.askyesno("确认", "确定删除这条提示词吗？", parent=dlg):
                listbox.delete(sel[0])

        def save_all():
            prompts = [listbox.get(i) for i in range(listbox.size())]
            self.config["action_prompts"] = prompts
            self._save_config()
            # 刷新所有文案框的下拉列表
            for entry in self.script_entries:
                if "prompt_combo" in entry:
                    entry["prompt_combo"]["values"] = prompts
            dlg.destroy()

        ttk.Button(btn_frame, text="➕ 新增", command=add_prompt).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="✏️ 编辑", command=edit_prompt).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🗑️ 删除", command=delete_prompt).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="保存全部", command=save_all).pack(side=tk.RIGHT, padx=5)

    def _import_scripts(self):
        current_count = len(self.script_entries)
        if current_count >= MAX_BATCH:
            messagebox.showwarning("提示", f"已达到最大文案数{MAX_BATCH}条")
            return

        file_path = filedialog.askopenfilename(
            title="选择文案文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            messagebox.showerror("错误", f"读取文件失败: {e}")
            return

        blocks = content.strip().split("\n\n")
        imported = 0
        persona_names = list(self.personas.keys())

        for block in blocks:
            if current_count + imported >= MAX_BATCH:
                break

            block = block.strip()
            if not block:
                continue

            persona = ""
            lines = block.split("\n")
            content_lines = []

            for line in lines:
                if line.startswith("人设:") or line.startswith("人设："):
                    persona = line.split(":", 1)[-1].split("：", 1)[-1].strip()
                else:
                    content_lines.append(line)

            text = "\n".join(content_lines).strip()
            if not text:
                continue

            self._add_script_box()

            entry = self.script_entries[-1]
            entry["text"].delete("1.0", tk.END)
            entry["text"].insert("1.0", text)

            if persona and persona in persona_names:
                entry["persona_combo"].set(persona)

            imported += 1

        if imported == 0:
            messagebox.showwarning("提示", "文件中未找到有效文案")
        else:
            self._log(f"批量导入: {imported}条文案")

    def _add_script_box(self):
        if len(self.script_entries) >= MAX_BATCH:
            messagebox.showwarning("提示", f"最多{MAX_BATCH}条文案")
            return

        idx = len(self.script_entries)
        frame = ttk.Frame(self.scripts_scrollable, relief=tk.GROOVE, borderwidth=1, padding=5)
        frame.pack(fill=tk.X, pady=3, padx=2)

        header = ttk.Frame(frame)
        header.pack(fill=tk.X)

        idx_label = ttk.Label(header, text=f"文案{idx + 1}:", font=("", 9, "bold"))
        idx_label.pack(side=tk.LEFT)

        persona_var = tk.StringVar()
        persona_combo = ttk.Combobox(header, textvariable=persona_var, state="readonly", width=10)
        persona_combo["values"] = list(self.personas.keys())
        persona_combo.pack(side=tk.LEFT, padx=5)

        img_label = ttk.Label(header, text="📷", foreground="gray", font=("", 8))
        img_label.pack(side=tk.LEFT, padx=3)

        aud_label = ttk.Label(header, text="🎤", foreground="gray", font=("", 8))
        aud_label.pack(side=tk.LEFT, padx=3)

        prompt_var = tk.StringVar()
        prompt_presets = self.config.get("action_prompts", []) if hasattr(self, "config") else []
        prompt_combo = ttk.Combobox(header, textvariable=prompt_var, width=20)
        prompt_combo["values"] = prompt_presets
        # v1.97：有预设就默认载入第一条，不用每次手动选；没有才显示占位符
        if prompt_presets:
            prompt_var.set(prompt_presets[0])
        else:
            prompt_combo.set("选择动作提示词...")
        # v1.54：仅第一个文案框在 prompt_combo 右边加"⚙️ 管理提示词"按钮
        prompt_mgr_btn = None
        if idx == 0:
            prompt_mgr_btn = ttk.Button(header, text="⚙️ 管理提示词", command=self._open_prompt_manager, width=12)
            prompt_mgr_btn.pack(side=tk.RIGHT, padx=5)
        prompt_combo.pack(side=tk.RIGHT, padx=5)
        # v1.52：保存 🎬 label 引用，便于 mode 切换时显隐
        prompt_label = ttk.Label(header, text="🎬", font=("", 8))
        prompt_label.pack(side=tk.RIGHT)

        if len(self.script_entries) >= 1:
            ttk.Button(header, text="删除", width=4, command=lambda f=frame: self._remove_script_box(f)).pack(side=tk.RIGHT, padx=2)

        height = self._calc_script_height(len(self.script_entries) + 1)
        txt = scrolledtext.ScrolledText(frame, height=height, font=("Microsoft YaHei", 9), wrap=tk.WORD)
        txt.pack(fill=tk.BOTH, expand=True, pady=2)

        entry = {
            "frame": frame,
            "text": txt,
            "persona_var": persona_var,
            "persona_combo": persona_combo,
            "prompt_var": prompt_var,
            "prompt_combo": prompt_combo,
            "prompt_label": prompt_label,  # v1.52：mode 切换时同步显隐
            "prompt_mgr_btn": prompt_mgr_btn,  # v1.54：仅 idx==0 时非 None
            "img_label": img_label,
            "aud_label": aud_label,
            "idx_label": idx_label,
        }
        self.script_entries.append(entry)

        persona_combo.bind("<<ComboboxSelected>>", lambda e, ent=entry: self._on_script_persona_change(ent))

        self._update_count()
        self._resize_all_script_boxes()
        # v1.52：新加的文案框也要按当前 mode 同步显隐字段
        if hasattr(self, "_apply_mode_visibility"):
            self._apply_mode_visibility()

    def _remove_script_box(self, frame):
        idx = None
        for i, e in enumerate(self.script_entries):
            if e["frame"] is frame:
                idx = i
                break
        if idx is not None and len(self.script_entries) > 1:
            frame.destroy()
            del self.script_entries[idx]
            self._update_count()
            self._relabel_script_boxes()
            self._resize_all_script_boxes()

    def _relabel_script_boxes(self):
        for i, entry in enumerate(self.script_entries):
            entry["idx_label"].config(text=f"文案{i + 1}:")

    # v1.52：按当前 mode 显隐字段
    # v1.60：yaml 里每个 mode 自己声明 ui.show_action_prompt，引擎按 yaml 决策
    def _apply_mode_visibility(self):
        try:
            _sel_name = self.mode_var.get()
        except Exception:
            return
        selected_mode = None
        for _m in (self.config or {}).get("modes", []) or []:
            if _m.get("name") == _sel_name:
                selected_mode = _m
                break
        # v1.60：按 mode.ui.show_action_prompt 决定（默认 true 向后兼容）
        show_action_prompt = True
        if selected_mode:
            show_action_prompt = selected_mode.get("ui", {}).get("show_action_prompt", True)
        for entry in self.script_entries:
            widgets = [entry.get("prompt_combo"), entry.get("prompt_label"), entry.get("prompt_mgr_btn")]
            widgets = [w for w in widgets if w is not None]
            if not show_action_prompt:
                for w in widgets:
                    try:
                        w.pack_forget()
                    except Exception:
                        pass
            else:
                # 顺序：⚙️ 按钮在最右，🎬 在它左边，prompt_combo 在 🎬 左边
                # pack_forget 后 widget manager 信息还在，重新 pack 即可恢复
                if entry.get("prompt_mgr_btn") is not None:
                    try:
                        entry["prompt_mgr_btn"].pack(side=tk.RIGHT, padx=5)
                    except Exception:
                        pass
                if entry.get("prompt_label") is not None:
                    try:
                        entry["prompt_label"].pack(side=tk.RIGHT)
                    except Exception:
                        pass
                if entry.get("prompt_combo") is not None:
                    try:
                        entry["prompt_combo"].pack(side=tk.RIGHT, padx=5)
                    except Exception:
                        pass

    def _resize_all_script_boxes(self):
        count = len(self.script_entries)
        height = self._calc_script_height(count)
        for entry in self.script_entries:
            entry["text"].config(height=height)

    def _update_count(self):
        self.count_label.config(text=f"{len(self.script_entries)}/{MAX_BATCH}")

    def _browse_dir(self, var):
        path = filedialog.askdirectory()
        if path:
            var.set(path)

    def _log(self, msg):
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)

    def _update_progress(self, pct, msg):
        def _update():
            self.progress_var.set(max(0, pct))
            if pct > 30:
                self.pct_label.config(text=f"{int(pct)}%")
            self.status_label.config(text=msg)
        self.root.after(0, _update)

    def _start_progress_timer(self, estimated_seconds, total_chars):
        self._stop_progress_timer()
        self._progress_start_time = time.time()
        self._progress_estimated_seconds = max(estimated_seconds, 1)
        self._progress_total_chars = total_chars
        est_min = estimated_seconds / 60
        self.progress_var.set(0)
        self.status_label.config(text=f"预计 {est_min:.1f} 分钟 (0%)，已用 0 分钟")

    def _update_progress_by_time(self):
        if not hasattr(self, "_progress_start_time") or not self._progress_start_time:
            return
        elapsed = time.time() - self._progress_start_time
        if self._progress_estimated_seconds <= 0:
            pct = 98
        else:
            ratio = min(elapsed / self._progress_estimated_seconds, 1.0)
            pct = int(ratio * 98)
        remaining = max(0, self._progress_estimated_seconds - elapsed)
        if pct >= 98:
            pct = 98
            msg = f"预估还剩 {remaining / 60:.1f} 分钟 (98%)"
            self.progress_var.set(pct)
            self.status_label.config(text=msg)
        elif pct >= 31:
            msg = f"预估还剩 {remaining / 60:.1f} 分钟 ({pct}%)"
            self.progress_var.set(pct)
            self.status_label.config(text=msg)

    def _stop_progress_timer(self):
        if hasattr(self, "_progress_timer_id") and self._progress_timer_id:
            self.root.after_cancel(self._progress_timer_id)
            self._progress_timer_id = None
        self._progress_start_time = None

    def _schedule_progress_update(self):
        if not hasattr(self, "_progress_start_time") or not self._progress_start_time:
            return
        self._update_progress_by_time()
        elapsed = time.time() - self._progress_start_time
        if self._progress_estimated_seconds > 0 and elapsed < self._progress_estimated_seconds:
            self._progress_timer_id = self.root.after(1000, self._schedule_progress_update)

    def _load_stats_history(self):
        history_file = os.path.join(_app_dir(), "stats_history.json")
        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_stats_history(self, history):
        history_file = os.path.join(_app_dir(), "stats_history.json")
        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _estimate_duration(self, char_count, persona, sample_audio):
        audio_time = char_count / 250 * 60
        if sample_audio and os.path.exists(sample_audio):
            try:
                sample_duration = _probe_duration(sample_audio)
                if sample_duration is None:
                    raise RuntimeError("no probe tool")
                sample_chars = self._get_sample_audio_chars(persona, sample_audio)
                if sample_chars > 0:
                    sample_speed = sample_chars / sample_duration
                    if sample_speed > 0:
                        audio_time = char_count / sample_speed
            except Exception:
                pass
        history = self._load_stats_history()
        persona_records = [h for h in history if h.get("persona") == persona and h.get("success")]
        if len(persona_records) >= 10:
            total_chars = sum(h["chars"] for h in persona_records)
            total_elapsed = sum(h["elapsed"] for h in persona_records)
            actual_video_ratio = (total_elapsed - (total_chars / 250 * 60)) / (total_chars / 250 * 60)
            if actual_video_ratio > 0:
                video_time = audio_time * actual_video_ratio
                return audio_time + video_time + 40
        video_time = audio_time * 24.5
        return audio_time + video_time + 40

    def _get_sample_audio_chars(self, persona, audio_path):
        p = self.personas.get(persona, {})
        for a in p.get("audios", []):
            if os.path.abspath(a.get("path", "")) == os.path.abspath(audio_path):
                return a.get("chars", 0)
        return 0

    def _should_record_stats(self, actual_elapsed, char_count, history):
        if len(history) < 10:
            return True
        avg_per_char = sum(h["elapsed"] / h["chars"] for h in history) / len(history)
        expected = avg_per_char * char_count
        deviation = abs(actual_elapsed - expected) / expected if expected > 0 else 0
        return deviation <= 0.50

    def _record_stats(self, persona, char_count, elapsed, success):
        if char_count < 50:
            return
        history = self._load_stats_history()
        if len(history) < 10 or self._should_record_stats(elapsed, char_count, history):
            history.append({
                "persona": persona,
                "chars": char_count,
                "elapsed": elapsed,
                "success": success,
                "timestamp": datetime.now().isoformat(),
            })
            if len(history) > 500:
                history = history[-500:]
            self._save_stats_history(history)

    def _open_output_dir(self):
        output_dir = self.output_dir_var.get().strip()
        if not output_dir or not os.path.exists(output_dir):
            messagebox.showwarning("提示", "输出目录不存在")
            return
        if sys.platform == "win32":
            os.startfile(output_dir)
        elif sys.platform == "darwin":
            subprocess.run(["open", output_dir])
        else:
            subprocess.run(["xdg-open", output_dir])

    def _on_run(self):
        output_dir = self.output_dir_var.get().strip()

        if not output_dir:
            messagebox.showwarning("提示", "请设置输出目录")
            return
        if not self.api_key_var.get().strip():
            messagebox.showwarning("提示", "请在设置页填入 API Key")
            return

        # v1.44 跨 Tab 互斥：如果别的 Tab 在跑，弹窗询问排队
        if self.task_queue.is_busy():
            current = self.task_queue.current_label()
            qsize = self.task_queue.queue_size()
            if not messagebox.askyesno(
                "任务互斥",
                f"⚠️ {current} 正在跑\n\n"
                f"📋 当前排队：{qsize} 个任务\n\n"
                f"选「是」加入排队\n选「否」取消本次运行"
            ):
                return
            display = f"任务 Tab（{len(self.script_entries)} 条文案）"
            self._log(f"📋 已加入排队（位置 {qsize + 1}）：{display}")
            self.task_queue.request(
                "任务 Tab", display,
                callback=self._do_run_batch,
                on_done=None,
            )
            return

        # 空闲，直接跑（包装一个 release 回调）
        display = f"任务 Tab（{len(self.script_entries)} 条文案）"
        self.task_queue.request(
            "任务 Tab", display,
            callback=self._do_run_batch,
            on_done=None,
        )

    def _do_run_batch(self):
        """v1.44：被 task_queue 调度的 callback（worker 内部 finally 会 release）"""
        threading.Thread(target=self._run_batch_worker, daemon=True).start()

    def _run_batch_worker(self):
        errors = []
        for i, entry in enumerate(self.script_entries):
            s = entry["text"].get("1.0", tk.END).strip()
            persona = entry["persona_var"].get().strip()
            if not persona:
                errors.append(f"文案{i + 1}: 未选择人设")
            if not s:
                errors.append(f"文案{i + 1}: 未输入文案")
            if persona and s:
                prompt = entry["prompt_var"].get().strip() if "prompt_var" in entry else ""
                if prompt == "选择动作提示词...":
                    prompt = ""
                tasks.append({"script": s, "persona": persona, "action_prompt": prompt, "idx": i})

        if errors:
            messagebox.showwarning("信息不完整", "所有文案框必须填写完整：\n\n" + "\n".join(errors))
            return
        if not tasks:
            messagebox.showwarning("提示", "请至少输入1条文案并选择人设")
            return

    def _run_batch_worker(self):
        """v1.44：从原 _on_run 抽出的 worker 函数（被 task_queue 调度执行）"""
        output_dir = self.output_dir_var.get().strip()

        tasks = []
        errors = []
        for i, entry in enumerate(self.script_entries):
            s = entry["text"].get("1.0", tk.END).strip()
            persona = entry["persona_var"].get().strip()
            if not persona:
                errors.append(f"文案{i + 1}: 未选择人设")
            if not s:
                errors.append(f"文案{i + 1}: 未输入文案")
            if persona and s:
                prompt = entry["prompt_var"].get().strip() if "prompt_var" in entry else ""
                if prompt == "选择动作提示词...":
                    prompt = ""
                tasks.append({"script": s, "persona": persona, "action_prompt": prompt, "idx": i})

        self._save_config()
        self.running = True
        self._write_lock(True)
        self._current_products = []
        self._refresh_product_list()
        self.run_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.progress_var.set(0)
        self.status_label.config(text="运行中...")
        self.notebook.select(3)

        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

        # v1.63：先 copy 完整 self.config，再覆盖 UI 输入 4 个变量
        # 防止 video_infinite_workflow / 以后新加的 api 字段被清掉（_save_config 同 bug 的第二副本）
        config = copy.deepcopy(self.config or {})
        config["api"] = dict(config.get("api", {}))
        config["api"].update({
            "key": self.api_key_var.get().strip(),
            "base_url": self.base_url_var.get().strip(),
            "audio_workflow": self.audio_wf_var.get().strip(),
            "video_workflow": self.video_wf_var.get().strip(),
        })
        config["max_concurrent"] = self.max_concurrent_var.get()
        self._log(f"[v1.63_debug] mode_wf={config['api'].get('video_infinite_workflow','<MISSING>')!r}")

        def on_task_complete(result):
            if result.get("output"):
                self._current_products.append(result["output"])
                self.root.after(0, self._refresh_product_list)

        def worker():
            try:
                batch_start = time.time()
                self.engine = KoboEngine(log_callback=self._log)
                self.engine.config = config
                self._stop_progress_timer()

                # v1.51：根据下拉框选中的 mode 决定走 audio_only 还是视频路径
                selected_mode = None
                _sel_name = self.mode_var.get()
                for _m in (self.config or {}).get("modes", []) or []:
                    if _m.get("name") == _sel_name:
                        selected_mode = _m
                        break
                if selected_mode is None:
                    # 兜底：找不到就当成老的"仅语音"判断兼容
                    is_audio_only = False
                    flow_kind = "video"
                    self._log(f"⚠️ 未匹配到模式「{_sel_name}」，按视频模式处理")
                else:
                    flow_kind = selected_mode.get("flow", "video")
                    is_audio_only = (flow_kind == "audio")
                if is_audio_only:
                    # 仅生成语音模式
                    if len(tasks) > 1:
                        self._log("仅生成语音模式：只处理第1条文案")
                    persona = tasks[0]["persona"]
                    char_count = len(tasks[0]["script"].replace(" ", "").replace("\n", ""))
                    _, sample_audio = self._pick_persona_files(persona)
                    if not sample_audio:
                        self._log(f"人设「{persona}」缺少声音文件")
                        self.root.after(0, lambda: messagebox.showerror("失败", f"人设「{persona}」缺少声音文件"))
                        self._stop_progress_timer()
                        return

                    self._log(f"人设: {persona}")
                    self._log(f"声音: {os.path.basename(sample_audio)}")
                    self._log(f"模式：仅生成语音")

                    result = self.engine.run_audio_only(
                        persona=persona, script=tasks[0]["script"],
                        sample_audio_path=sample_audio,
                        output_dir=output_dir,
                        progress_callback=self._update_progress,
                    )
                    self._stop_progress_timer()
                    self.progress_var.set(100)
                    self.status_label.config(text="完成")
                    if result["status"] == "SUCCESS":
                        on_task_complete(result)
                        self._record_stats(persona, char_count, result["elapsed"], True)
                        self.root.after(0, lambda: messagebox.showinfo("完成", f"语音生成成功!\n\n时长: {result['duration']:.1f}秒\n耗时: {result['elapsed']}秒\n\n{result['output']}"))
                    else:
                        self._record_stats(persona, char_count, 0, False)
                        self.root.after(0, lambda: messagebox.showerror("失败", f"生成失败:\n{result.get('error', '未知错误')}"))
                elif len(tasks) == 1:
                    persona = tasks[0]["persona"]
                    char_count = len(tasks[0]["script"].replace(" ", "").replace("\n", ""))
                    self._log(f"[v1.70_debug] flow_kind={flow_kind!r}, tasks={len(tasks)}, mode_sel_name={_sel_name!r}")
                    # v1.67：Heygem 模式走专属路径（不取图片，取形象视频+参考音）
                    if flow_kind == "heygem":
                        self._log("[v1.67_heygem_branch] ✅ 进入 Heygem 专属分支")
                        video_path, ref_audio_path = self._pick_persona_heygem_files(persona)
                        if not video_path or not ref_audio_path:
                            self._log(f"人设「{persona}」Heygem 资源缺失（需要形象视频+样本声音）")
                            self.root.after(0, lambda: messagebox.showerror(
                                "失败", f"人设「{persona}」缺少形象视频或样本声音\n\n请到「人设管理」勾选后再跑"))
                            self._stop_progress_timer()
                            return
                        self._log(f"人设: {persona}")
                        self._log(f"形象视频: {os.path.basename(video_path)}")
                        self._log(f"参考声音: {os.path.basename(ref_audio_path)}")
                        self._progress_timer_id = None
                        self._progress_start_time = time.time()
                        self._progress_estimated_seconds = 0
                        result = self.engine.run_heygem(
                            persona=persona, prompt=tasks[0]["script"],
                            video_path=video_path, ref_audio_path=ref_audio_path,
                            output_dir=output_dir,
                            progress_callback=self._update_progress,
                        )
                        self._stop_progress_timer()
                        self.progress_var.set(100)
                        self.status_label.config(text="完成")
                        if result["status"] == "SUCCESS":
                            on_task_complete(result)
                            self._record_stats(persona, char_count, result["elapsed"], True)
                            self.root.after(0, lambda: messagebox.showinfo(
                                "完成", f"Heygem 生成成功!\n\n耗时: {result['elapsed']}秒\n\n{result['output']}"))
                        else:
                            self._record_stats(persona, char_count, 0, False)
                            self.root.after(0, lambda: messagebox.showerror(
                                "失败", f"生成失败:\n{result.get('error', '未知错误')}"))
                        return  # heygem 走完跳出
                    image_path, sample_audio = self._pick_persona_files(persona)
                    if not image_path or not sample_audio:
                        self._log(f"人设「{persona}」文件缺失")
                        self.root.after(0, lambda: messagebox.showerror("失败", f"人设「{persona}」缺少图片或声音"))
                        self._stop_progress_timer()
                        return

                    estimated_duration = self._estimate_duration(char_count, persona, sample_audio)
                    self._log(f"预估耗时: {estimated_duration / 60:.1f}分钟")
                    self._start_progress_timer(estimated_duration, char_count)
                    self.root.after(500, self._schedule_progress_update)

                    self._log(f"人设: {persona}")
                    self._log(f"图片: {os.path.basename(image_path)}")
                    self._log(f"声音: {os.path.basename(sample_audio)}")

                    result = self.engine.run(
                        persona=persona, script=tasks[0]["script"],
                        image_path=image_path, sample_audio_path=sample_audio,
                        output_dir=output_dir, action_prompt=tasks[0].get("action_prompt", ""),
                        progress_callback=self._update_progress,
                        mode=flow_kind,  # v1.58：传递 mode 给 engine
                    )
                    self._stop_progress_timer()
                    self.progress_var.set(100)
                    self.status_label.config(text="完成")
                    if result["status"] == "SUCCESS":
                        on_task_complete(result)
                        self._record_stats(persona, char_count, result["elapsed"], True)
                        self.root.after(0, lambda: messagebox.showinfo("完成", f"视频生成成功!\n\n时长: {result['duration']:.1f}秒\n耗时: {result['elapsed'] // 60:.1f}分钟\n\n{result['output']}"))
                    else:
                        self._record_stats(persona, char_count, 0, False)
                        self.root.after(0, lambda: messagebox.showerror("失败", f"生成失败:\n{result.get('error', '未知错误')}"))
                else:
                    # v1.71：多文案不再挡 heygem/infinite，全部走批量路径
                    self.progress_var.set(0)
                    self.status_label.config(text="音频阶段，并发处理中...")
                    self._stop_progress_timer()
                    self._progress_start_time = time.time()
                    self._progress_estimated_seconds = 0
                    self._progress_timer_id = None

                    self._log(f"批量模式: {len(tasks)}条文案, {self.max_concurrent_var.get()}并发")
                    if flow_kind == "heygem":
                        # v1.71：heygem 模式批量（不走音频阶段，直接每个任务调一次 run_heygem 并发）
                        results = self.engine.run_heygem_batch(
                            tasks=tasks,
                            output_dir=output_dir,
                            pick_fn=self._pick_persona_heygem_files,
                            progress_callback=self._update_progress,
                            task_complete_callback=on_task_complete,
                        )
                    else:
                        # audio_only / full_video / video_infinite 都走通用 batch（v1.71 加 mode 参数让 infinite 选对工作流）
                        results = self.engine.run_batch(
                            tasks=tasks,
                            output_dir=output_dir,
                            pick_fn=self._pick_persona_files,
                            progress_callback=self._update_progress,
                            task_complete_callback=on_task_complete,
                            mode=flow_kind,
                        )

                    self._stop_progress_timer()
                    self.progress_var.set(100)
                    self.status_label.config(text="完成")

                    success_count = sum(1 for r in results if r["status"] == "SUCCESS")
                    fail_count = sum(1 for r in results if r["status"] != "SUCCESS")
                    batch_elapsed = time.time() - batch_start
                    batch_minutes = batch_elapsed / 60

                    self.root.after(0, lambda: messagebox.showinfo("批量完成", f"批量任务执行完毕!\n\n成功: {success_count}\n失败: {fail_count}\n总耗时: {batch_minutes:.1f}分钟"))

            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("错误", str(e)))
            finally:
                self.running = False
                self._write_lock(False)
                self.root.after(0, self._reset_ui)
                # v1.44：worker 结束 → 释放全局队列（任务 Tab）
                try:
                    self.task_queue.release()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_stop(self):
        if self.engine:
            self.engine.stop()
        self.running = False
        self._write_lock(False)
        self._log("用户请求停止，正在取消服务端任务...")
        self._cleanup_incomplete_workspace()

        def force_exit():
            time.sleep(5)
            if self.running:
                self._log("停止超时，强制退出")
                os._exit(0)

        threading.Thread(target=force_exit, daemon=True).start()

    def _cleanup_incomplete_workspace(self):
        output_dir = self.output_dir_var.get().strip()
        if not output_dir or not os.path.exists(output_dir):
            return
        workspace = os.path.join(output_dir, "workspace")
        if os.path.exists(workspace):
            import shutil
            try:
                shutil.rmtree(workspace, ignore_errors=True)
                self._log("未完成的临时文件已清理")
            except Exception:
                pass

    def _on_close(self):
        if self.engine:
            self.engine.stop()
        self.running = False
        self._stop_progress_timer()
        self.progress_var.set(0)
        self.status_label.config(text="就绪")
        self._cleanup_incomplete_workspace()
        self._log("软件关闭，已取消服务端任务并清理残留")
        self._remove_lock()
        self.root.destroy()
        os._exit(0)

    def _kill_stale_processes(self):
        """检测旧实例 → 弹 yes/no → 杀旧进程 → 清缓存 → 归零 → 写新锁"""
        old_pid = None
        old_running = False

        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, "r") as f:
                    content = f.read().strip()
                for line in content.split("\n"):
                    if line.startswith("PID:"):
                        old_pid = int(line.split(":")[1].strip())
                    elif line.startswith("RUNNING:"):
                        old_running = line.split(":")[1].strip() == "True"
                if old_pid and not self._is_pid_alive(old_pid):
                    old_pid = None
                    try:
                        os.remove(LOCK_FILE)
                    except Exception:
                        pass
            except Exception:
                old_pid = None
                try:
                    os.remove(LOCK_FILE)
                except Exception:
                    pass

        if not old_pid:
            old_pid = self._find_old_process_pid()

        if old_pid:
            if old_running:
                msg = "软件正在生成视频，强制重启可能丢失进度，确定重启吗？"
            else:
                msg = "软件已在运行，是否重启？"
            try:
                root_tmp = tk.Tk()
                root_tmp.withdraw()
                root_tmp.update_idletasks()
                root_tmp.attributes("-topmost", True)
                confirm = messagebox.askyesno("提示", msg, parent=root_tmp)
                root_tmp.destroy()
            except Exception:
                confirm = True
            if not confirm:
                os._exit(0)
            try:
                os.kill(old_pid, 9)
            except Exception:
                pass
            time.sleep(1)

            # 顶掉旧实例后：清理缓存 + 归零
            try:
                self._clear_cache()
            except Exception as e:
                print(f"[清缓存] {e}", flush=True)
            try:
                self._reset_to_zero()
            except Exception as e:
                print(f"[归零] {e}", flush=True)

        try:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception:
            pass

        self._write_lock(False)

    def _clear_cache(self):
        """清理缓存：__pycache__、.pyc、缩略图、临时文件"""
        cleared = {"pycache": 0, "pyc": 0, "thumb": 0, "tmp": 0}
        app_dir = _app_dir()
        for root, dirs, files in os.walk(app_dir):
            for d in list(dirs):
                if d == "__pycache__":
                    p = os.path.join(root, d)
                    try:
                        shutil.rmtree(p, ignore_errors=True)
                        cleared["pycache"] += 1
                    except Exception:
                        pass
                    dirs.remove(d)
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    try:
                        os.remove(os.path.join(root, f))
                        cleared["pyc"] += 1
                    except Exception:
                        pass
        tmp = tempfile.gettempdir()
        try:
            for name in os.listdir(tmp):
                if name.startswith("_kobo_thumb_") and name.endswith(".png"):
                    try:
                        os.remove(os.path.join(tmp, name))
                        cleared["thumb"] += 1
                    except Exception:
                        pass
                elif name.startswith("kobo_") and name.endswith((".tmp", ".lock~")):
                    try:
                        os.remove(os.path.join(tmp, name))
                        cleared["tmp"] += 1
                    except Exception:
                        pass
        except Exception:
            pass
        total = sum(cleared.values())
        if total > 0:
            print(f"[清缓存] 清理完成: {cleared}", flush=True)

    def _reset_to_zero(self):
        """归零：把运行期累计状态重置（不动用户的 config/personas/material）"""
        stats_file = os.path.join(_app_dir(), "stats_history.json")
        try:
            with open(stats_file, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False)
        except Exception:
            pass
        print("[归零] stats_history.json → []", flush=True)

    def _find_old_process_pid(self):
        """多模式匹配：dev 模式（python + main.py）和 .app 模式（二进制名）"""
        my_pid = os.getpid()
        patterns = ["main.py", "可乐混剪", "可乐口播", "kobo_video"]
        if sys.platform == "darwin":
            for pat in patterns:
                try:
                    result = subprocess.run(
                        ["pgrep", "-f", pat],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0:
                        for line in result.stdout.strip().split("\n"):
                            line = line.strip()
                            if not line.isdigit():
                                continue
                            pid = int(line)
                            if pid != my_pid and self._is_pid_alive(pid):
                                return pid
                except Exception:
                    pass
        elif sys.platform == "win32":
            for pat in patterns:
                try:
                    result = subprocess.run(
                        ["wmic", "process", "where", f"commandline like '%{pat}%'", "get", "processid"],
                        capture_output=True, text=True, timeout=5,
                    )
                    for line in result.stdout.strip().split("\n"):
                        line = line.strip()
                        if line.isdigit():
                            pid = int(line)
                            if pid != my_pid:
                                return pid
                except Exception:
                    pass
        return None

    def _is_pid_alive(self, pid):
        try:
            if sys.platform == "win32":
                result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5)
                return str(pid) in result.stdout
            else:
                os.kill(pid, 0)
                return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False

    def _build_global_status_bar(self):
        """v1.44：底部全局状态条，显示当前任务 + 队列"""
        try:
            bar = ttk.Frame(self.root)
            bar.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=(0, 3))
            self._global_status_label = ttk.Label(
                bar, text="🟢 空闲", font=("", 9), foreground="#4a90d9"
            )
            self._global_status_label.pack(side=tk.LEFT, padx=5)
            self._global_queue_label = ttk.Label(
                bar, text="", font=("", 9), foreground="#888"
            )
            self._global_queue_label.pack(side=tk.LEFT, padx=5)
            self._global_cancel_btn = ttk.Button(
                bar, text="❌ 取消所有排队", command=self._cancel_all_queued,
                state=tk.DISABLED
            )
            self._global_cancel_btn.pack(side=tk.RIGHT, padx=5)
        except Exception as e:
            print(f"[v1.44] 全局状态条初始化失败：{e}")

    def _refresh_global_status(self):
        """刷新全局状态条文字。task_queue 完成后会调用。"""
        try:
            current = self.task_queue.current_label()
            qsize = self.task_queue.queue_size()
            # v1.47：同时写文件 debug log（stdout 在 dev 模式不可靠）
            try:
                import datetime as _dt
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] [DBG-MAIN] _refresh_global_status current={current!r}, qsize={qsize}\n")
            except Exception:
                pass
            if current is None:
                self._global_status_label.config(text="🟢 空闲", foreground="#4a90d9")
                self._global_queue_label.config(text="")
                self._global_cancel_btn.config(state=tk.DISABLED)
            else:
                self._global_status_label.config(
                    text=f"🔵 正在跑：{current}", foreground="#e67e22"
                )
                if qsize > 0:
                    self._global_queue_label.config(text=f"📋 排队：{qsize} 个")
                    self._global_cancel_btn.config(state=tk.NORMAL)
                else:
                    self._global_queue_label.config(text="")
                    self._global_cancel_btn.config(state=tk.DISABLED)
        except Exception:
            pass

    def _cancel_all_queued(self):
        """用户点'取消所有排队'按钮"""
        n = self.task_queue.cancel_all_queued()
        if n > 0:
            self._log(f"🗑 已清空 {n} 个排队任务")
            messagebox.showinfo("已取消", f"已清空 {n} 个排队任务\n当前任务跑完后不会再启动新任务")
        else:
            messagebox.showinfo("提示", "当前没有排队任务")

    def _on_output_dir_changed(self, *_):
        """v1.44：成品 Tab 改 output_dir 时同步到 tab5 + 持久化到 config.yaml"""
        value = self.output_dir_var.get().strip()
        if not value:
            return
        defaults = self.config.setdefault("defaults", {})
        defaults["output_dir"] = value
        self._save_config()
        # 同步给 tab5
        if hasattr(self, "merge_tab") and self.merge_tab is not None:
            try:
                current = self.merge_tab.output_dir.get().strip()
                if current != value:
                    self.merge_tab.output_dir.set(value)
            except Exception:
                pass

    def _write_lock(self, running):
        try:
            with open(LOCK_FILE, "w") as f:
                f.write(f"PID:{os.getpid()}\nRUNNING:{running}\n")
        except Exception:
            pass

    def _remove_lock(self):
        try:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception:
            pass

    def _reset_ui(self):
        self.run_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        if not self.running:
            self.status_label.config(text="就绪")
            self.pct_label.config(text="0%")
            self._refresh_product_list()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = KoboApp()
    app.run()
