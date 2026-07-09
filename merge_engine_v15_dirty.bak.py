"""
可乐口播 v1.5 - 混剪引擎
=======================

按 /Applications/可乐混剪.app (laozheng-cutter) 的逻辑实现：
- 数据模型：MixConfig / InsertPoint(10 字段) / VideoFile
- ffmpeg filter_complex 模板 严格 照抄 cola-cutter 的 src/ffmpeg.rs
- ffmpeg 编码参数 严格 照抄 app（libx264 -preset medium -crf 20 + aac -b:a 192k）
- 默认用 app 自带的 ffmpeg（/Applications/可乐混剪.app/.../ffmpeg-darwin-x64）

字段命名完全跟 Rust 端 serde 一致：
  MixConfig:    mainFolder, mainVideos, insertPoints, outputFolder,
                outputSuffix, outputSettings, mainCleanupMode
  InsertPoint:  startTime, endTime, folder, videos, cleanupMode,
                silent, layout, pipScale, orientation, resolution
  VideoFile:    name, success, outputPath, error, duration

枚举值（严格跟 Rust 端一致）：
  Layout:       replaced, pip_center, pip_top, pip_bottom,
                pip_top_left, pip_top_right, pip_bottom_left, pip_bottom_right
  Orientation:  landscape, portrait
  Resolution:   720p, 1080p
  CleanupMode:  scan_folder, get_video_duration, archive_to_subfolder, delete_to_trash
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any, Tuple


# ============================================================
# 数据模型（按 Rust 端 serde 字段名 1:1 映射）
# ============================================================

LAYOUT_OPTIONS = [
    "replaced", "pip_center", "pip_top", "pip_bottom",
    "pip_top_left", "pip_top_right", "pip_bottom_left", "pip_bottom_right",
]

# UI 显示标签（用户看着舒服的中文，内部 enum 仍是英文，跟 Rust 端兼容）
LAYOUT_LABELS = {
    "replaced":        "替换主材画面（黑边填）",
    "pip_center":      "画中画-居中",
    "pip_top":         "画中画-顶部居中",
    "pip_bottom":      "画中画-底部居中",
    "pip_top_left":    "画中画-左上",
    "pip_top_right":   "画中画-右上",
    "pip_bottom_left": "画中画-左下",
    "pip_bottom_right":"画中画-右下",
}

ORIENTATION_OPTIONS = ["landscape", "portrait"]
ORIENTATION_LABELS = {
    "landscape": "横屏 16:9",
    "portrait":  "竖屏 9:16",
}

RESOLUTION_OPTIONS = ["720p", "1080p"]
RESOLUTION_LABELS = {
    "720p":  "720P (高清)",
    "1080p": "1080P (超清)",
}

CLEANUP_OPTIONS = [
    "scan_folder",
    "get_video_duration",
    "archive_to_subfolder",
    "delete_to_trash",
]
CLEANUP_LABELS = {
    "scan_folder":          "扫描文件夹（不动）",
    "get_video_duration":   "解析视频时长（不动）",
    "archive_to_subfolder": "移到子文件夹（默认 used/）",
    "delete_to_trash":      "删除到回收站",
}

# 横竖屏+分辨率 → 实际像素
RESOLUTION_MAP = {
    ("landscape", "720p"):  (1280, 720),
    ("landscape", "1080p"): (1920, 1080),
    ("portrait", "720p"):   (720, 1280),
    ("portrait", "1080p"):  (1080, 1920),
}


@dataclass
class VideoFile:
    """一个视频文件（输入或输出都共用）。"""
    name: str = ""
    success: bool = False
    outputPath: str = ""
    error: str = ""
    duration: float = 0.0


@dataclass
class InsertPoint:
    """一个插入点配置（严格 10 字段，对应 Rust InsertPoint + 前端备注 note）。"""
    startTime: float = 0.0          # 开始时间（秒）
    endTime: float = 0.0            # 结束时间（秒，0 = 不限）
    folder: str = ""                # 素材文件夹路径
    videos: List[VideoFile] = field(default_factory=list)
    cleanupMode: str = "scan_folder"
    silent: bool = False            # 插入素材无声（True=无原声）
    layout: str = "replaced"        # 8 个值
    pipScale: float = 0.3           # PIP 大小 0.1 - 0.8
    orientation: str = "landscape"
    resolution: str = "720p"
    note: str = ""                  # 前端备注（不参与混剪引擎逻辑）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "startTime": self.startTime,
            "endTime": self.endTime,
            "folder": self.folder,
            "videos": [asdict(v) for v in self.videos],
            "cleanupMode": self.cleanupMode,
            "silent": self.silent,
            "layout": self.layout,
            "pipScale": self.pipScale,
            "orientation": self.orientation,
            "resolution": self.resolution,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InsertPoint":
        return cls(
            startTime=float(d.get("startTime", 0.0)),
            endTime=float(d.get("endTime", 0.0)),
            folder=d.get("folder", ""),
            videos=[VideoFile(**v) for v in d.get("videos", [])],
            cleanupMode=d.get("cleanupMode", "scan_folder"),
            silent=bool(d.get("silent", False)),
            layout=d.get("layout", "replaced"),
            pipScale=float(d.get("pipScale", 0.3)),
            orientation=d.get("orientation", "landscape"),
            resolution=d.get("resolution", "720p"),
            note=d.get("note", ""),
        )


@dataclass
class OutputSettings:
    orientation: str = "landscape"
    resolution: str = "720p"


@dataclass
class MixConfig:
    """整套配置（对应 Rust MixConfig）。"""
    mainFolder: str = ""
    mainVideos: List[VideoFile] = field(default_factory=list)
    insertPoints: List[InsertPoint] = field(default_factory=list)
    outputFolder: str = ""
    outputSuffix: str = "_混剪"
    outputSettings: OutputSettings = field(default_factory=OutputSettings)
    mainCleanupMode: str = "scan_folder"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mainFolder": self.mainFolder,
            "mainVideos": [asdict(v) for v in self.mainVideos],
            "insertPoints": [p.to_dict() for p in self.insertPoints],
            "outputFolder": self.outputFolder,
            "outputSuffix": self.outputSuffix,
            "outputSettings": asdict(self.outputSettings),
            "mainCleanupMode": self.mainCleanupMode,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MixConfig":
        return cls(
            mainFolder=d.get("mainFolder", ""),
            mainVideos=[VideoFile(**v) for v in d.get("mainVideos", [])],
            insertPoints=[InsertPoint.from_dict(p) for p in d.get("insertPoints", [])],
            outputFolder=d.get("outputFolder", ""),
            outputSuffix=d.get("outputSuffix", "_混剪"),
            outputSettings=OutputSettings(**d.get("outputSettings", {})),
            mainCleanupMode=d.get("mainCleanupMode", "scan_folder"),
        )


# ============================================================
# 时间转换（界面显示分:秒.百分秒 ↔ 内部存秒 f64）
# ============================================================

def seconds_to_mmss(t: float) -> Tuple[int, int, int]:
    """秒 → (分, 秒, 百分秒)，方便 Spinbox 显示。"""
    if t < 0:
        t = 0
    m = int(t // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        cs = 0
        s += 1
        if s >= 60:
            s = 0
            m += 1
    return m, s, cs


def mmss_to_seconds(m: int, s: int, cs: int) -> float:
    """(分, 秒, 百分秒) → 秒。"""
    return float(m) * 60.0 + float(s) + float(cs) / 100.0


# ============================================================
# ffmpeg 路径
# ============================================================

# App 自带的 ffmpeg（最高优先级，app 已经验证过兼容性）
APP_BUNDLED_FFMPEG = "/Applications/可乐混剪.app/Contents/Resources/_up_/bin/ffmpeg-darwin-x64"


def find_ffmpeg() -> Optional[str]:
    """
    找 ffmpeg 二进制。优先级：
      1) App 自带的（最高优先，app 验证过兼容）
      2) PATH
      3) /opt/homebrew/bin、/usr/local/bin
    """
    # 1. App 自带
    if Path(APP_BUNDLED_FFMPEG).exists():
        return APP_BUNDLED_FFMPEG
    # 2. PATH
    p = shutil.which("ffmpeg")
    if p and Path(p).exists():
        return p
    # 3. 系统路径
    for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if Path(p).exists():
            return p
    return None


# ============================================================
# 视频时长探测
# ============================================================

def probe_duration(ffmpeg: str, video_path: str) -> float:
    """用 ffmpeg 探测时长（秒）。"""
    try:
        result = subprocess.run(
            [ffmpeg, "-i", video_path],
            capture_output=True, text=True, timeout=30,
        )
        # ffmpeg 信息打到 stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
        if m:
            h, mi, s = m.group(1), m.group(2), m.group(3)
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


# ============================================================
# 核心：构造 filter_complex 拼接一条主视频
# ============================================================
#
# 模板严格照抄 cola-cutter (Rust) 的 src/ffmpeg.rs：
#
#   视频缩放模板 A（直接缩放到目标尺寸，lanczos 插值）:
#     scale=W:H:flags=lanczos,setpts=PTS-STARTPTS[X]
#
#   视频缩放模板 B（保持比例缩放 + 黑边填充）:
#     scale=W:H:force_original_aspect_ratio=decrease:eval=frame,
#     pad=W:H:(ow-iw)/2:(oh-ih)/2:color=black,
#     setpts=PTS-STARTPTS[X]
#
#   音频模板（强制 44100Hz 立体声 fltp）:
#     asetpts=PTS-STARTPTS,aresample=44100,
#     aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[A]
#
#   静音模板:
#     anullsrc=r=44100:cl=stereo[A]
#
#   concat 模板:
#     [v0][v1]...[vN]concat=n=N+1:v=1:a=0[outv]
#     [a0][a1]...[aN]concat=n=N+1:v=0:a=1[outa]
#
#   编码参数（严格照抄 app）:
#     -c:v libx264 -preset medium -crf 20
#     -c:a aac -b:a 192k
#     -movflags +faststart

def _v_filter_scale_pad(out_w: int, out_h: int) -> str:
    """视频模板 B：保持比例 + 黑边填。"""
    return (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:eval=frame,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setpts=PTS-STARTPTS"
    )


def _v_filter_scale_lanczos(out_w: int, out_h: int) -> str:
    """视频模板 A：直接缩放。"""
    return (
        f"scale={out_w}:{out_h}:flags=lanczos,"
        f"setpts=PTS-STARTPTS"
    )


def _a_filter_stereo() -> str:
    """音频模板：强制 44100Hz 立体声。"""
    return (
        "asetpts=PTS-STARTPTS,aresample=44100,"
        "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    )


def _a_filter_silent() -> str:
    """静音模板（不携带原素材音频）。"""
    return "anullsrc=r=44100:cl=stereo"


def _overlay_position(main_w: int, main_h: int, overlay_w: int, overlay_h: int, position: str) -> str:
    """按 layout 算 overlay 坐标。"""
    if position == "pip_center":
        return f"(main_w-overlay_w)/2:(main_h-overlay_h)/2"
    if position == "pip_top":
        return f"(main_w-overlay_w)/2:0"
    if position == "pip_bottom":
        return f"(main_w-overlay_w)/2:main_h-overlay_h"
    if position == "pip_top_left":
        return f"0:0"
    if position == "pip_top_right":
        return f"main_w-overlay_w:0"
    if position == "pip_bottom_left":
        return f"0:main_h-overlay_h"
    if position == "pip_bottom_right":
        return f"main_w-overlay_w:main_h-overlay_h"
    return f"(main_w-overlay_w)/2:(main_h-overlay_h)/2"


def build_filter_complex(
    main_path: str,
    insert_points: List[InsertPoint],
    out_w: int,
    out_h: int,
    main_input_idx: int = 0,
) -> Tuple[str, List[str]]:
    """
    构造 filter_complex 拼接一条主视频（app 风格的 segment + concat 结构）。

    逻辑（按时间顺序拼接）：
      主视频前段 (0 ~ insert[0].startTime)
        → 素材1 (insert[0].folder 里的视频)
        → 主视频中段 (insert[0].endTime ~ insert[1].startTime)
        → 素材2
        → ...
        → 主视频末段

    返回 (filter_complex_str, [所有 input 路径])
    """
    # 1) 扫所有插入点对应的素材文件夹，建立"插入点 idx → 素材 input idx"映射
    video_exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
    # material_inputs 是 [path, ...]，按 ip 出现顺序
    material_paths: List[str] = []
    # ip_to_mat_idx: ip 在 sort 后 list 里的位置 → 在 material_paths 里的位置
    # 我们直接用 input 顺序号就行
    ip_to_mat_idx: Dict[int, int] = {}

    sorted_ips = sorted(insert_points, key=lambda x: x.startTime)
    for ip_idx, ip in enumerate(sorted_ips):
        if not ip.folder or not Path(ip.folder).exists():
            continue
        mats = []
        for f in sorted(Path(ip.folder).iterdir()):
            if f.is_file() and f.suffix.lower() in video_exts:
                mats.append(str(f))
        if not mats:
            continue
        # 这个 ip 的素材是 material_paths 的最后一段
        start_idx = len(material_paths)
        material_paths.extend(mats)
        ip_to_mat_idx[ip_idx] = start_idx  # 第一个素材的 input idx

    # 2) input 列表：0=主视频, 1..N=素材
    inputs = ["-i", main_path]
    for mp in material_paths:
        inputs.extend(["-i", mp])

    # 3) 构造 segment 列表
    segments: List[Dict[str, Any]] = []  # 每个 segment: {kind, params, label_v, label_a}
    cursor = 0.0
    for ip_idx, ip in enumerate(sorted_ips):
        if ip.startTime < cursor:
            continue  # 重叠，跳过
        # 主视频段 cursor ~ ip.startTime
        if ip.startTime > cursor:
            segments.append({
                "kind": "main",
                "start": cursor,
                "end": ip.startTime,
            })
        # 素材段
        if ip_idx in ip_to_mat_idx:
            segments.append({
                "kind": "mat",
                "ip": ip,
                "mat_idx": ip_to_mat_idx[ip_idx],  # 第一个素材的 input idx
            })
        # endTime=0 = 不限长，用主视频中 ip.startTime~+5s 占位（避免无限循环）
        cursor = ip.endTime if ip.endTime > 0 else ip.startTime + 5.0

    # 主视频末段
    segments.append({"kind": "main_last", "start": cursor})

    # 4) 构造 filter_complex
    filters: List[str] = []

    for idx, seg in enumerate(segments):
        v_label = f"v{idx}"
        a_label = f"a{idx}"

        if seg["kind"] in ("main", "main_last"):
            start = seg["start"]
            end = seg.get("end", -1)
            # 主视频裁剪 + 缩放 + 黑边填（app 模板 B）
            trim_v = f"trim=start={start}"
            if end is not None and end > 0:
                trim_v += f":end={end}"
            v_filter = (
                f"[{main_input_idx}:v]{trim_v},{_v_filter_scale_pad(out_w, out_h)}"
                f"[{v_label}]"
            )
            filters.append(v_filter)

            # 音频
            trim_a = f"atrim=start={start}"
            if end is not None and end > 0:
                trim_a += f":end={end}"
            a_filter = (
                f"[{main_input_idx}:a]{trim_a},{_a_filter_stereo()}"
                f"[{a_label}]"
            )
            filters.append(a_filter)

        else:
            ip: InsertPoint = seg["ip"]
            mat_idx = seg["mat_idx"]
            mat_input_idx = mat_idx + 1  # +1 因为 input 0 是主视频

            duration = ip.endTime - ip.startTime if ip.endTime > 0 else 5.0
            layout = ip.layout

            if layout == "replaced":
                # 替换主材画面：直接用素材填满（app 模板 B + pad）
                v_filter = (
                    f"[{mat_input_idx}:v]{_v_filter_scale_pad(out_w, out_h)}"
                    f"[{v_label}]"
                )
                filters.append(v_filter)

                # 音频
                if ip.silent:
                    a_filter = f"{_a_filter_silent()}[{a_label}]"
                else:
                    a_filter = (
                        f"[{mat_input_idx}:a]atrim=0:duration={duration},"
                        f"{_a_filter_stereo()}"
                        f"[{a_label}]"
                    )
                filters.append(a_filter)
            else:
                # PIP：先把主视频放好（作为底图），再 overlay 缩小后的素材
                # 这一段比较特殊：需要用 overlay 合成 main+mat，不能直接给一个 label
                # 解决：先准备主视频底图 v_base 和素材 PIP v_pip，然后 overlay 成 v_label
                v_base_label = f"vbase{idx}"
                v_pip_label = f"vpip{idx}"
                # 主视频底图（裁剪 + scale+pad）
                v_base = (
                    f"[{main_input_idx}:v]trim=start={ip.startTime}:end={ip.endTime if ip.endTime > 0 else ip.startTime + duration},"
                    f"{_v_filter_scale_pad(out_w, out_h)}"
                    f"[{v_base_label}]"
                )
                # 素材 PIP 缩放（app 模板 A，lanczos）
                pip_w = int(round(out_w * ip.pipScale))
                pip_h = int(round(out_h * ip.pipScale))
                v_pip = (
                    f"[{mat_input_idx}:v]{_v_filter_scale_lanczos(pip_w, pip_h)}"
                    f"[{v_pip_label}]"
                )
                # overlay 合成
                pos = _overlay_position(out_w, out_h, pip_w, pip_h, layout)
                v_overlay = (
                    f"[{v_base_label}][{v_pip_label}]overlay=x={pos}:y={pos.split(':')[1] if ':' in pos else pos}:format=auto:eof_action=pass"
                    f"[{v_label}]"
                )
                # ❌ 上面 x=pos 错了，应该是 x=pos.x, y=pos.y
                pos_x, pos_y = pos.split(":")
                v_overlay = (
                    f"[{v_base_label}][{v_pip_label}]overlay=x={pos_x}:y={pos_y}:format=auto:eof_action=pass"
                    f"[{v_label}]"
                )
                filters.extend([v_base, v_pip, v_overlay])

                # PIP 音频：用素材原声（或静音）
                if ip.silent:
                    a_filter = f"{_a_filter_silent()}[{a_label}]"
                else:
                    a_filter = (
                        f"[{mat_input_idx}:a]atrim=0:duration={duration},"
                        f"{_a_filter_stereo()}"
                        f"[{a_label}]"
                    )
                filters.append(a_filter)

    # 5) concat 拼接所有段
    n_segments = len(segments)
    if n_segments == 0:
        return "", inputs

    v_labels = "".join(f"[v{i}]" for i in range(n_segments))
    a_labels = "".join(f"[a{i}]" for i in range(n_segments))
    filters.append(f"{v_labels}concat=n={n_segments}:v=1:a=0[outv]")
    filters.append(f"{a_labels}concat=n={n_segments}:v=0:a=1[outa]")

    # 6) 合成完整 filter_complex
    full_filter = ";".join(filters)
    return full_filter, inputs


def run_ffmpeg_mix(
    ffmpeg: str,
    main_path: str,
    insert_points: List[InsertPoint],
    output_path: str,
    orientation: str = "landscape",
    resolution: str = "720p",
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """
    跑一次 ffmpeg 拼接。返回 (success, error_msg)。

    ffmpeg 命令严格照抄 /Applications/可乐混剪.app：
        -c:v libx264 -preset medium -crf 20
        -c:a aac -b:a 192k
        -movflags +faststart
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    out_w, out_h = RESOLUTION_MAP.get((orientation, resolution), (1280, 720))

    filter_complex, inputs = build_filter_complex(
        main_path, insert_points, out_w, out_h,
    )

    # 严格照抄 app 的 ffmpeg 命令（不擅自加 profile/level/pix_fmt）
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    cmd.extend(inputs)
    cmd.extend(["-filter_complex", filter_complex])
    cmd.extend([
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ])

    log(f"▶ ffmpeg 命令（前 8 段）: {' '.join(cmd[:8])}")
    log(f"▶ 完整命令: {' '.join(cmd)}")
    log(f"  filter_complex: {filter_complex[:300]}...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            err = result.stderr[-1500:] if result.stderr else "未知错误"
            log(f"❌ ffmpeg 失败 (退出码 {result.returncode})")
            log(f"  stderr: {err}")
            return False, err
        log(f"✅ 完成 → {output_path}")
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "ffmpeg 超时（> 600s）"
    except Exception as e:
        return False, str(e)


# ============================================================
# 用过素材处理
# ============================================================

def handle_used_material(
    used_path: str,
    mode: str,
    subfolder_name: str = "used",
    log_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """
    按 CleanupMode 处理已用过的素材：
      - archive_to_subfolder: 移到 <folder>/<subfolder_name>/
      - delete_to_trash:      移到 ~/.Trash/
      - scan_folder / get_video_duration: 不动
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    src = Path(used_path)
    if not src.exists():
        return False

    if mode == "archive_to_subfolder":
        folder = src.parent / subfolder_name
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / src.name
        # 避免重名
        i = 1
        while dst.exists():
            dst = folder / f"{src.stem}_{i}{src.suffix}"
            i += 1
        try:
            shutil.move(str(src), str(dst))
            log(f"  📦 已用素材归档: {src.name} → {dst.parent.name}/")
            return True
        except Exception as e:
            log(f"  ⚠️ 归档失败: {e}")
            return False
    elif mode == "delete_to_trash":
        trash = Path.home() / ".Trash"
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / src.name
        i = 1
        while dst.exists():
            dst = trash / f"{src.stem}_{i}{src.suffix}"
            i += 1
        try:
            shutil.move(str(src), str(dst))
            log(f"  🗑 已用素材移到回收站: {src.name}")
            return True
        except Exception as e:
            log(f"  ⚠️ 删除失败: {e}")
            return False
    else:
        # scan_folder / get_video_duration: 不动
        return True


# ============================================================
# 调度器：批量跑多条主视频
# ============================================================

class MixScheduler:
    """批量跑混剪任务。"""

    def __init__(
        self,
        config: MixConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        on_finished: Optional[Callable[[bool], None]] = None,
    ):
        self.config = config
        self.log_cb = log_callback or (lambda m: None)
        self.progress_cb = progress_callback or (lambda cur, total, name: None)
        self.on_finished = on_finished  # 跑完/停止/异常 都调，参数 success: bool
        self._stop_flag = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stopped = False  # 是否被用户手动停止
        self._exception: Optional[BaseException] = None  # 异常保存

    def log(self, msg: str):
        self.log_cb(msg)

    def stop(self):
        self._stop_flag.set()
        self._stopped = True

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def was_stopped(self) -> bool:
        """是否被用户手动停止（用于 GUI 恢复按钮时判断状态文案）。"""
        return self._stopped

    def start(self):
        if self.is_running():
            return
        self._stop_flag.clear()
        self._stopped = False
        self._exception = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _scan_main_videos(self) -> List[str]:
        """扫主视频文件夹下所有视频。"""
        exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
        if not self.config.mainFolder or not Path(self.config.mainFolder).exists():
            return []
        files = []
        for f in sorted(Path(self.config.mainFolder).iterdir()):
            if f.is_file() and f.suffix.lower() in exts:
                # 跳过输出文件（避免重复处理）
                if self.config.outputSuffix and self.config.outputSuffix in f.stem:
                    continue
                files.append(str(f))
        return files

    def _run(self):
        success = True
        try:
            self._run_impl()
        except Exception as e:
            success = False
            self._exception = e
            self.log(f"❌ 调度异常: {e}")
        finally:
            # 无论成功/失败/异常，都通知 GUI
            if self.on_finished:
                try:
                    self.on_finished(success)
                except Exception:
                    pass

    def _run_impl(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            self.log("❌ 找不到 ffmpeg！请安装 ffmpeg 或把它加入 PATH。")
            return

        self.log(f"✅ ffmpeg: {ffmpeg}")

        # 1) 扫主视频
        main_videos = self._scan_main_videos()
        if not main_videos:
            self.log("⚠️ 主视频文件夹下没有可处理的视频。")
            return

        self.log(f"📂 找到 {len(main_videos)} 条主视频待处理")

        # 2) 确定输出目录
        output_folder = self.config.outputFolder or self.config.mainFolder
        Path(output_folder).mkdir(parents=True, exist_ok=True)

        # 3) 每条主视频跑一遍
        for i, main_path in enumerate(main_videos, 1):
            if self._stop_flag.is_set():
                self.log("⏹ 已停止")
                break

            main_name = Path(main_path).stem
            suffix = self.config.outputSuffix or "_混剪"
            output_name = f"{main_name}{suffix}.mp4"
            output_path = str(Path(output_folder) / output_name)

            self.log(f"\n─── [{i}/{len(main_videos)}] {Path(main_path).name} ───")
            self.progress_cb(i, len(main_videos), main_name)

            # 探测主视频时长
            duration = probe_duration(ffmpeg, main_path)
            self.log(f"  ⏱ 主视频时长: {duration:.2f}s")

            # 校验插入点时间
            valid_ips = []
            for ip in self.config.insertPoints:
                if ip.startTime < 0:
                    self.log(f"  ⚠️ 跳过非法插入点（startTime<0）: {ip.folder}")
                    continue
                if ip.endTime > 0 and ip.endTime <= ip.startTime:
                    self.log(f"  ⚠️ 跳过非法插入点（end<=start）: {ip.folder}")
                    continue
                if ip.startTime >= duration and duration > 0:
                    self.log(f"  ⚠️ 跳过插入点（超出主视频时长）: {ip.folder}")
                    continue
                valid_ips.append(ip)

            if not valid_ips:
                self.log("  ⚠️ 没有有效插入点，跳过此视频")
                continue

            # 跑 ffmpeg
            ok, err = run_ffmpeg_mix(
                ffmpeg,
                main_path,
                valid_ips,
                output_path,
                self.config.outputSettings.orientation,
                self.config.outputSettings.resolution,
                log_callback=self.log,
            )

            if ok:
                # 处理用过的素材（按每个插入点自己的 cleanupMode）
                for ip in valid_ips:
                    if not ip.folder or not Path(ip.folder).exists():
                        continue
                    for f in Path(ip.folder).iterdir():
                        if f.is_file() and f.suffix.lower() in {".mp4", ".mov", ".mkv"}:
                            handle_used_material(
                                str(f), ip.cleanupMode,
                                log_callback=self.log,
                            )

        self.log(f"\n🏁 全部完成！处理 {len(main_videos)} 条主视频")
        self.progress_cb(len(main_videos), len(main_videos), "完成")
