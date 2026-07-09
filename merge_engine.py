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
    layout: str = "replaced"        # replaced / pip_top_left / pip_top_right / pip_bottom_left / pip_bottom_right
    pipScale: float = 0.3           # PIP 大小 0.1 - 0.8（百分比 10-80）
    pipMargin: int = 20             # PIP 边距，距画布像素（仅画中画生效）
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
            "pipMargin": self.pipMargin,
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
            pipMargin=int(d.get("pipMargin", 20)),
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
    mainArchiveDir: str = ""  # v1.49：主视频归档目录（AI 推送时用原主视频文件夹）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mainFolder": self.mainFolder,
            "mainVideos": [asdict(v) for v in self.mainVideos],
            "insertPoints": [p.to_dict() for p in self.insertPoints],
            "outputFolder": self.outputFolder,
            "outputSuffix": self.outputSuffix,
            "outputSettings": asdict(self.outputSettings),
            "mainCleanupMode": self.mainCleanupMode,
            "mainArchiveDir": self.mainArchiveDir,  # v1.49
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
            mainArchiveDir=d.get("mainArchiveDir", ""),  # v1.49
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

# v2.02：切换到新版 brew ffmpeg。
# 之前用 /Applications/可乐混剪.app/Contents/Resources/_up_/bin/ffmpeg-darwin-x64
# （2018 tessus build，x86_64，70MB），怀疑与 tab6 批量推 Tab5 时全部 ok=False 相关。
# 现在优先用 /usr/local/bin/ffmpeg（用户升级过的 brew 8.1.2，库路径已对齐）。
# 如果 brew ffmpeg 不存在再 fallback 到老路径。
APP_BUNDLED_FFMPEG = "/usr/local/bin/ffmpeg"
APP_BUNDLED_FFMPEG_LEGACY = "/Applications/可乐混剪.app/Contents/Resources/_up_/bin/ffmpeg-darwin-x64"


def find_ffmpeg() -> Optional[str]:
    """
    找 ffmpeg 二进制。优先级：
      0) 项目 bin/ffmpeg.bak_tessus（v2.10.16 调试用，存在则优先；删除该文件即回退原优先级）
      1) 项目 APP_BUNDLED_FFMPEG（最高优先，默认 /usr/local/bin/ffmpeg）
      2) PATH
      3) /opt/homebrew/bin、/usr/local/bin
      4) 老版 app 自带 ffmpeg（最后兜底，避免 v2.02 之前的环境出错）
    """
    # 0. 临时测试 tessus 软切换（v2.10.16 调试用，存在 .bak_tessus 就用它）
    project_bin = Path(__file__).resolve().parent / "bin" / "ffmpeg.bak_tessus"
    if project_bin.exists():
        return str(project_bin)
    # 1. 项目指定路径（默认新版 brew ffmpeg）
    if Path(APP_BUNDLED_FFMPEG).exists():
        return APP_BUNDLED_FFMPEG
    # 2. 老版 app 自带（兜底，避免一些环境没 brew 的机器崩）
    if Path(APP_BUNDLED_FFMPEG_LEGACY).exists():
        return APP_BUNDLED_FFMPEG_LEGACY
    # 3. PATH
    p = shutil.which("ffmpeg")
    if p and Path(p).exists():
        return p
    # 4. 系统路径
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
            capture_output=True, text=True, timeout=30, start_new_session=True,  # v2.10.33: 脱离父进程 session
        )
        # ffmpeg 信息打到 stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
        if m:
            h, mi, s = m.group(1), m.group(2), m.group(3)
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def probe_main_size(ffmpeg: str, video_path: str) -> Tuple[int, int]:
    """用 ffmpeg 探测主视频尺寸 (width, height)。失败返回 (0, 0)。"""
    try:
        result = subprocess.run(
            [ffmpeg, "-i", video_path],
            capture_output=True, text=True, timeout=30, start_new_session=True,  # v2.10.33: 脱离父进程 session
        )
        # 例: "Stream #0:0(und): Video: h264 ... 1280x720 ..."
        m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", result.stderr)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 0, 0


def calc_output_size(main_w: int, main_h: int, resolution: str) -> Tuple[int, int]:
    """按主视频原比例 + 档位短边算输出尺寸（无黑边）。

    规则（v1.10）：
      - 档位 720P → 短边 = 720
      - 档位 1080P → 短边 = 1080
      - 长边按原视频比例等比算
      - 短边 < 档位 → 放大（保持原比例）
      - 短边 >= 档位 → 缩小或不缩（保持原比例）
      - 长边结果保证偶数（libx264 要求）
    """
    if main_w <= 0 or main_h <= 0:
        return (1280, 720) if resolution != "1080p" else (1920, 1080)

    target_short = 720 if resolution == "720p" else 1080
    src_short = min(main_w, main_h)
    src_long = max(main_w, main_h)

    scale = target_short / src_short
    out_short = target_short
    out_long = int(round(src_long * scale))

    # 偶数对齐
    if out_long % 2 != 0:
        out_long += 1

    # 还原成正确方向
    if main_w >= main_h:
        return (out_long, out_short)
    else:
        return (out_short, out_long)


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
    """视频模板 B：保持比例 + 黑边填（给画中画 replaced 用）。"""
    return (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:eval=frame,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setpts=PTS-STARTPTS"
    )


def _v_filter_scale_proportional(out_w: int, out_h: int) -> str:
    """视频模板 C：按比例直接缩放，无黑边（v1.10 主视频专用）。

    因为 out_w/out_h 已经按主视频原比例算过（见 calc_output_size），
    所以不会有黑边，直接 lanczos 缩放即可。
    """
    return (
        f"scale={out_w}:{out_h}:flags=lanczos,"
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


def _overlay_position(main_w: int, main_h: int, overlay_w: int, overlay_h: int, position: str, margin: int = 20) -> str:
    """按 layout + margin 算 overlay 坐标（ffmpeg overlay 表达式字符串）。

    pip_center 忽略 margin（纯粹按 PIP 大小居中）。
    其他 4 个角按 margin 偏移到对应邻边。
    """
    if position == "pip_center":
        return f"(main_w-overlay_w)/2:(main_h-overlay_h)/2"
    m = max(0, int(margin))
    if position == "pip_top_left":
        return f"{m}:{m}"
    if position == "pip_top_right":
        return f"main_w-overlay_w-{m}:{m}"
    if position == "pip_bottom_left":
        return f"{m}:main_h-overlay_h-{m}"
    if position == "pip_bottom_right":
        return f"main_w-overlay_w-{m}:main_h-overlay_h-{m}"
    # replaced / 其它默认：左上角不偏移（铺满场景不会走这里）
    return f"{m}:{m}"


def build_filter_complex(
    main_path: str,
    insert_points,
    out_w: int,
    out_h: int,
    main_input_idx: int = 0,
) -> Tuple[str, List[str], List[str]]:
    """
    构造 filter_complex（画中画覆盖模型，素材"从头播"模型）。

    用户的实际混剪逻辑：
      - 主视频从头到尾连续播放（最底层）
      - 插入素材**永远从头开始播**（不是从主视频的 startTime 时刻开始）
      - 用 overlay 的 enable='between(t,startTime,endTime)' 控制只在时间窗内显示
      - endTime > 0：素材只播 (endTime-startTime) 秒后被 trim 截断
      - endTime == 0：素材从 startTime 起一直显示到素材自己播完（不限长）
      - 用 setpts=PTS-STARTPTS+startTime/TB 把素材时间戳偏移到主视频的 startTime 位置
      - 多个插入点并行：插入点 i+1 的图层比 i 靠前，遮挡 i
      - 插入素材的音频用 atrim 截 + adelay 偏移到 startTime

    关键：
      - 素材永远从开头帧播，不会因为素材只有 5 秒而无法在 6-8s 时间窗显示
      - 素材用 trim 截到时间窗长度，setpts 偏移时间戳
    """
    # 1) 扫插入点对应的素材文件夹
    video_exts = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}
    material_paths: List[str] = []
    ip_to_mat_idx: Dict[int, int] = {}

    sorted_ips = sorted(insert_points, key=lambda x: x.startTime)
    for ip_idx, ip in enumerate(sorted_ips):
        if not ip.folder or not Path(ip.folder).exists():
            continue
        mats = []
        for f in sorted(Path(ip.folder).iterdir()):
            # v2.05：跳过 macOS 在外置硬盘上生成的 ._AppleDouble 隐藏文件
            if f.name.startswith("._"):
                continue
            if f.is_file() and f.suffix.lower() in video_exts:
                mats.append(str(f))
        if not mats:
            continue
        already_used = {material_paths[mi] for mi in ip_to_mat_idx.values()}
        available = [m for m in mats if m not in already_used]
        if not available:
            # 素材不够了——明确报错，不能让同一素材被两次插入
            raise RuntimeError(
                f"素材不够：插入点 #{ip_idx+1} 需要新素材，但 {ip.folder} "
                f"里只有 {len(mats)} 个且都被分配过。"
                f"请加素材/减少插入点/或换一个文件夹。"
            )
        chosen = available[ip_idx % len(available)]
        if chosen not in material_paths:
            material_paths.append(chosen)
        ip_to_mat_idx[ip_idx] = material_paths.index(chosen)

    # 2) input 列表
    inputs = ["-i", main_path]
    for mp in material_paths:
        inputs.extend(["-i", mp])

    # 3) 主视频 → [base0]（v1.10 用 proportional 算法，按原比例无黑边）
    filters: List[str] = [
        f"[{main_input_idx}:v]{_v_filter_scale_proportional(out_w, out_h)}[base0]"
    ]

    # 4) 每个插入点一层 overlay
    overlay_count = 0
    mat_audio_labels: List[str] = []

    for ip_idx, ip in enumerate(sorted_ips):
        if ip_idx not in ip_to_mat_idx:
            continue
        mat_input_idx = ip_to_mat_idx[ip_idx] + 1
        layout = ip.layout

        # 时间窗：endTime=0 表示不限长（素材从 startTime 起一直显示到素材自己播完）
        if ip.endTime > 0:
            window_dur = max(0.0, ip.endTime - ip.startTime)
            enable_expr = f"enable='between(t,{ip.startTime},{ip.endTime})'"
        else:
            window_dur = 0.0  # 不限时不 trim，用素材原始长度
            enable_expr = f"enable='gte(t,{ip.startTime})'"

        # 素材位置 / 缩放
        if layout == "replaced":
            mat_scale = _v_filter_scale_pad(out_w, out_h)
            pos_x, pos_y = "0", "0"
        else:
            pip_w = int(round(out_w * ip.pipScale))
            if pip_w % 2 != 0:
                pip_w += 1
            # 画中画：宽度固定 pip_w，高度按素材原比例算（ih*pip_w/iw）
            # 这样画中画 stream 实际像素 = 显示像素，没有内黑边
            # overlay 算位置用的 overlay_w 也是实际像素，到画布各边距离 = 用户设的 m
            mat_scale = (
                f"scale={pip_w}:ih*{pip_w}/iw:flags=lanczos,"
                f"setpts=PTS-STARTPTS"
            )
            pos = _overlay_position(out_w, out_h, pip_w, pip_w, layout, ip.pipMargin)
            pos_x, pos_y = pos.split(":")

        # 素材：scale → 可选 trim 到时间窗长度 → setpts 偏移到 startTime
        # 这样素材"永远从头播"，但 overlay 显示的帧对应主视频的 startTime 时刻
        ovl_label = f"ovl{overlay_count}"
        if window_dur > 0:
            mat_v_chain = (
                f"[{mat_input_idx}:v]{mat_scale},"
                f"trim=duration={window_dur},"
                f"setpts=PTS-STARTPTS+{ip.startTime}/TB"
                f"[{ovl_label}]"
            )
        else:
            # 不限：素材原长
            mat_v_chain = (
                f"[{mat_input_idx}:v]{mat_scale},"
                f"setpts=PTS-STARTPTS+{ip.startTime}/TB"
                f"[{ovl_label}]"
            )
        filters.append(mat_v_chain)

        # overlay：base_i + ovl_i → base_{i+1}
        current_base = f"base{overlay_count}"
        next_base = f"base{overlay_count + 1}"
        filters.append(
            f"[{current_base}][{ovl_label}]"
            f"overlay=x={pos_x}:y={pos_y}:{enable_expr}:eof_action=pass"
            f"[{next_base}]"
        )

        # 素材音频：silent=False 才处理，atrim 截 + adelay 偏移到 startTime
        if not ip.silent:
            mat_a_label = f"mat_a{overlay_count}"
            delay_ms = int(round(ip.startTime * 1000))
            if window_dur > 0:
                a_chain = (
                    f"[{mat_input_idx}:a]atrim=0:duration={window_dur},"
                    f"{_a_filter_stereo()},"
                    f"adelay={delay_ms}|{delay_ms}"
                    f"[{mat_a_label}]"
                )
            else:
                a_chain = (
                    f"[{mat_input_idx}:a]{_a_filter_stereo()},"
                    f"adelay={delay_ms}|{delay_ms}"
                    f"[{mat_a_label}]"
                )
            filters.append(a_chain)
            mat_audio_labels.append(mat_a_label)

        overlay_count += 1

    # 5) 输出视频 = 最后一层 base
    if overlay_count == 0:
        filters.append("[base0]copy[outv]")
    else:
        last_base = f"base{overlay_count}"
        filters.append(f"[{last_base}]copy[outv]")

    # 6) 主音频
    filters.append(f"[{main_input_idx}:a]{_a_filter_stereo()}[main_a]")

    # 7) 音频合成
    if mat_audio_labels:
        mix_labels = "[main_a]" + "".join(f"[{l}]" for l in mat_audio_labels)
        n_inputs = 1 + len(mat_audio_labels)
        filters.append(
            f"{mix_labels}amix=inputs={n_inputs}:duration=first"
            f":dropout_transition=0[outa]"
        )
    else:
        filters.append("[main_a]acopy[outa]")

    full_filter = ";".join(filters)

    # 把用到的素材路径挂到函数对象上
    build_filter_complex.last_used_paths = list(material_paths)

    return full_filter, inputs, list(material_paths)


def _stage_inputs_to_local(
    main_path: str,
    insert_points: List[InsertPoint],
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[str, List[InsertPoint], dict, Optional[Any]]:
    """
    v2.05 staging：把所有要用的视频（主视频 + 每个插入点选中的素材）
    先复制一份到内置 SSD 的临时目录，ffmpeg 只读 SSD，避开 USB 移动硬盘
    并发读时的 moov atom not found 问题。

    返回：
      staged_main_path:   临时目录里的主视频路径
      staged_insert_points: 替换 folder 指向临时目录的 InsertPoint 列表
      folder_map:          {staged_folder_path: real_folder_path}（v2.07 新增）
      cleanup_ctx:        退出时调 cleanup_ctx.cleanup() 真删临时目录

    v2.07 fix：folder_map 让 used_paths 在 archive 前换回真实路径，
    否则 handle_used_material 把素材搬到 /tmp 下，然后被 cleanup 真删——用户看不到。

    如果全部文件原本就在 SSD（无 USB），folder_map 为空 dict。
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    def is_on_usb(p: str) -> bool:
        return p.startswith("/Volumes/") and not p.startswith("/Volumes/Macintosh HD")

    def has_usb_path(s: str) -> bool:
        return is_on_usb(s)

    needs_stage = is_on_usb(main_path)
    if not needs_stage:
        for ip in insert_points:
            if ip.folder and has_usb_path(ip.folder):
                needs_stage = True
                break

    if not needs_stage:
        # 全在 SSD 上，根本不用 staging（避免无谓 IO）
        return main_path, insert_points, {}, None

    # 创建临时 staging 目录
    import tempfile as _tempfile
    import uuid as _uuid
    stage_dir = Path(_tempfile.gettempdir()) / f"kele_stage_{_uuid.uuid4().hex[:8]}"
    stage_dir.mkdir(parents=True, exist_ok=True)

    def _copy_in(src: str, tag: str) -> str:
        """复制一个视频到 stage_dir，返回新路径。失败抛异常。"""
        src_p = Path(src)
        if not src_p.exists():
            raise FileNotFoundError(f"staging 源不存在：{src}")
        dst = stage_dir / f"{tag}_{src_p.name}"
        shutil.copy2(str(src_p), str(dst))
        return str(dst)

    # v2.07: staged 文件夹 → 真实文件夹 映射，给 handle_used_material 用
    folder_map: Dict[str, str] = {}

    try:
        staged_main = _copy_in(main_path, "main")
        log(f"📦 staging: 主视频 → {staged_main}")
        folder_map[staged_main] = main_path  # 主视频的前缀是 staged_main 自身

        staged_ips = []
        for ip in insert_points:
            new_ip = InsertPoint(
                startTime=ip.startTime,
                endTime=ip.endTime,
                folder=ip.folder,
                videos=ip.videos,
                cleanupMode=ip.cleanupMode,
                silent=ip.silent,
                layout=ip.layout,
                pipScale=ip.pipScale,
                orientation=ip.orientation,
                resolution=ip.resolution,
            )
            # v2.05：把整个 ip.folder 也复制到 stage 目录
            if ip.folder and has_usb_path(ip.folder):
                stage_folder = stage_dir / f"matfolder_{Path(ip.folder).name}"
                if not stage_folder.exists():
                    shutil.copytree(ip.folder, stage_folder)
                    log(f"📦 staging: 素材文件夹 → {stage_folder}")
                new_ip.folder = str(stage_folder)
                # v2.07: 记录映射
                folder_map[str(stage_folder)] = ip.folder  # 真实文件夹
                # staged 文件路径 = staged_folder/xxx.mp4，真实文件路径 = ip.folder/xxx.mp4
                # resolved: 任意 staged 路径 → 把 staged_folder 前缀换回 ip.folder
            staged_ips.append(new_ip)

        class _Cleanup:
            def __init__(self, d: Path):
                self.d = d
            def cleanup(self_inner):
                if self_inner.d.exists():
                    shutil.rmtree(self_inner.d, ignore_errors=True)

        return staged_main, staged_ips, folder_map, _Cleanup(stage_dir)
    except Exception as e:
        # staging 失败 → 清掉临时目录，**不抛错**，退回原路径（让原逻辑继续）
        log(f"⚠️ staging 失败，回退原路径：{e}")
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        return main_path, insert_points, {}, None


def run_ffmpeg_mix(
    ffmpeg: str,
    main_path: str,
    insert_points: List[InsertPoint],
    output_path: str,
    orientation: str = "landscape",
    resolution: str = "720p",
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str, List[str]]:
    """
    跑一次 ffmpeg 拼接。返回 (success, error_msg)。

    ffmpeg 命令严格照抄 /Applications/可乐混剪.app：
        -c:v libx264 -preset medium -crf 20
        -c:a aac -b:a 192k
        -movflags +faststart

    v2.05：自动把 USB 上的视频复制到内置 SSD 临时目录，ffmpeg 读 SSD。
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    # v2.05 staging：先把 USB 上的视频搬到 SSD 临时目录
    # v2.07 拿 folder_map 让 used_paths 换回真实路径（否则搬 used/ 时搬到 staging）
    main_path, insert_points, folder_map, _cleanup = _stage_inputs_to_local(
        main_path, insert_points, log_callback=log,
    )

    def _resolve_real(p: str) -> str:
        """把 staged 路径换回真实 USB 路径，让 handle_used_material 搬到 USB 上"""
        for staged_folder, real_folder in folder_map.items():
            if staged_folder and (p == staged_folder or p.startswith(staged_folder + "/") or p.startswith(staged_folder + os.sep)):
                return p.replace(staged_folder, real_folder, 1)
        return p

    # v1.10: 探测主视频实际尺寸，按原比例 + 档位算输出尺寸（无黑边）
    main_w, main_h = probe_main_size(ffmpeg, main_path)
    if main_w > 0 and main_h > 0:
        out_w, out_h = calc_output_size(main_w, main_h, resolution)
        log(f"📐 主视频 {main_w}×{main_h} → 输出 {out_w}×{out_h}（{orientation}, {resolution}）")
    else:
        # 探测失败 → 退回旧 RESOLUTION_MAP（兼容）
        out_w, out_h = RESOLUTION_MAP.get((orientation, resolution), (1280, 720))
        log(f"⚠️ 主视频尺寸探测失败，使用默认 {out_w}×{out_h}")

    filter_complex, inputs, used_paths = build_filter_complex(
        main_path, insert_points, out_w, out_h,
    )

    # v2.07: 把 used_paths 里的 staged 路径换回 USB 真实路径，让 handle_used_material 搬到 USB
    used_paths = [_resolve_real(p) for p in used_paths] if folder_map else used_paths

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
            start_new_session=True,  # v2.10.33: 脱离父进程 session
        )
        if result.returncode != 0:
            err = result.stderr[-1500:] if result.stderr else "未知错误"
            log(f"❌ ffmpeg 失败 (退出码 {result.returncode})")
            log(f"  stderr: {err}")
            # v2.01 调试：失败时把 stderr 写到 /tmp/kele_debug.log，方便诊断（GUI 日志被刷掉）
            try:
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%H:%M:%S")
                with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{_ts}] [DBG-FFMPEG-FAIL] exit={result.returncode}, main={main_path!r}, output={output_path!r}\n")
                    f.write(f"[{_ts}] [DBG-FFMPEG-FAIL] stderr: {err}\n")
            except Exception:
                pass
            return False, err, []
        log(f"✅ 完成 → {output_path}")
        return True, "", used_paths
    except subprocess.TimeoutExpired:
        # v2.01 调试：超时也写文件
        try:
            import datetime as _dt
            _ts = _dt.datetime.now().strftime("%H:%M:%S")
            with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{_ts}] [DBG-FFMPEG-TIMEOUT] main={main_path!r}\n")
        except Exception:
            pass
        return False, "ffmpeg 超时（> 600s）", []
    except Exception as e:
        try:
            import datetime as _dt
            _ts = _dt.datetime.now().strftime("%H:%M:%S")
            with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{_ts}] [DBG-FFMPEG-EXC] {type(e).__name__}: {e}, main={main_path!r}\n")
        except Exception:
            pass
        return False, str(e), []
    finally:
        # v2.05 staging cleanup：跑完就真删临时目录
        if _cleanup is not None:
            try:
                _cleanup.cleanup()
                log(f"🧹 staging 临时目录已清理")
            except Exception:
                pass


# GUI 用的 cleanupMode 标识 → handle_used_material 的 mode 映射
GUI_TO_CLEANUP_MODE = {
    "move":  "archive_to_subfolder",
    "trash": "delete_to_trash",
    "keep":  "scan_folder",
}


# ============================================================
# 用过素材处理
# ============================================================

def handle_used_material(
    used_path: str,
    mode: str,
    subfolder_name: str = "used",
    log_callback: Optional[Callable[[str], None]] = None,
    archive_dir: Optional[str] = None,
) -> bool:
    """
    按 CleanupMode 处理已用过的素材：
      - archive_to_subfolder: 移到 <folder>/<subfolder_name>/
      - delete_to_trash:      移到 ~/.Trash/
      - scan_folder / get_video_duration: 不动

    v1.80 用户设计意图（澄清版）：
      - tab6 选「移到子目录」= 策略：tab5 跑完后**真把视频从原位置搬到 used/**，
        原位置必须空出来。下次再要用同一个视频，得从 used/ 拷回去。
      - 关键：处理 symlink src（auto_run 模式的 tmp）时，必须 follow symlink，
        把真实文件搬走，**原位置（archive_dir 顶层）真清空**，而不是只挪走 tmp 软链接。
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    src = Path(used_path)
    if not src.exists() and not src.is_symlink():
        return False

    # v1.80：解出真实文件路径（如果是 symlink）。
    # 这是核心改动——之前 v1.79 只 copy2(real, dst) 然后 unlink(symlink)，
    # 真实文件**留在 archive_dir 顶层不动**，违背用户「真搬到 used/」的意图。
    # 现在 follow symlink 后直接对真实文件操作（move 或 unlink），原位置真清空。
    is_symlink_src = src.is_symlink()
    if is_symlink_src:
        real_src = Path(os.path.realpath(str(src)))
        if not real_src.exists():
            log(f"  ⚠️ symlink target 已不存在: {real_src}")
            return False
    else:
        real_src = src

    if mode == "archive_to_subfolder":
        # v1.80：决定目标目录——直接用 real_src.parent 做根，
        # 不再用 is_tmp 判断。auto_run 模式 symlink 在 /var/folders/... 但真实文件在 archive_dir/，
        # 真实文件搬家后归档目录就是 archive_dir/used/，符合用户期望。
        folder = real_src.parent / subfolder_name
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / real_src.name
        # 避免重名
        i = 1
        while dst.exists() or dst.is_symlink():
            dst = folder / f"{real_src.stem}_{i}{real_src.suffix}"
            i += 1
        try:
            # v1.80 debug：pre/post
            try:
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%H:%M:%S")
                with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{_ts}] [DBG-HUM v1.80] PRE: src={str(src)!r}, islink={is_symlink_src}, real_src={str(real_src)!r}, real_exists={real_src.exists()}, dst={str(dst)!r}, dst_exists={dst.exists()}\n")
            except Exception:
                pass
            # v1.80：真搬真实文件到 dst（不是搬 symlink 也不是 copy）。
            # 同设备用 os.replace（原子、重命名，最快）；跨设备用 shutil.move。
            try:
                os.replace(str(real_src), str(dst))
            except OSError:
                # 跨设备 fallback
                shutil.move(str(real_src), str(dst))
            # 如果 src 是 symlink（auto_run 模式），symlink 已经因为 real_src 被搬走而失效，
            # 清理掉（os.replace 已自动 unlink 同 inode，但显式清更稳）
            if is_symlink_src:
                try:
                    if src.exists() or src.is_symlink():
                        os.unlink(str(src))
                except FileNotFoundError:
                    pass
            try:
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%H:%M:%S")
                with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{_ts}] [DBG-HUM v1.80] POST: real_exists={real_src.exists()}, dst_exists={dst.exists()}, dst_size={dst.stat().st_size if dst.exists() else 0}\n")
            except Exception:
                pass
            log(f"  📦 已用素材归档: {real_src.name} → {dst.parent.name}/")
            return True
        except Exception as e:
            log(f"  ⚠️ 归档失败: {e}")
            try:
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%H:%M:%S")
                with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{_ts}] [DBG-HUM v1.80] EXC: {e!r}\n")
            except Exception:
                pass
            return False
    elif mode == "delete_to_trash":
        trash = Path.home() / ".Trash"
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / real_src.name
        i = 1
        while dst.exists():
            dst = trash / f"{real_src.stem}_{i}{real_src.suffix}"
            i += 1
        try:
            os.replace(str(real_src), str(dst))
            if is_symlink_src:
                try:
                    os.unlink(str(src))
                except FileNotFoundError:
                    pass
            log(f"  🗑 已用素材移到回收站: {real_src.name}")
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

            # v2.01 调试：把 valid_ips 详情写文件，方便诊断
            try:
                import datetime as _dt
                _ts = _dt.datetime.now().strftime("%H:%M:%S")
                with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{_ts}] [DBG-VALID-IPS] main={main_path!r}, duration={duration:.2f}, "
                            f"total_ips={len(self.config.insertPoints)}, valid_ips={len(valid_ips)}\n")
                    for _ip in valid_ips:
                        f.write(f"[{_ts}] [DBG-VALID-IPS]   ip: start={_ip.startTime}, end={_ip.endTime}, folder={_ip.folder!r}\n")
                    for _skip_ip in self.config.insertPoints:
                        if _skip_ip not in valid_ips:
                            f.write(f"[{_ts}] [DBG-VALID-IPS]   skipped: start={_skip_ip.startTime}, end={_skip_ip.endTime}, folder={_skip_ip.folder!r}\n")
            except Exception:
                pass

            if not valid_ips:
                self.log("  ⚠️ 没有有效插入点，跳过此视频")
                continue

            # 跑 ffmpeg
            ok, err, used_paths = run_ffmpeg_mix(
                ffmpeg,
                main_path,
                valid_ips,
                output_path,
                self.config.outputSettings.orientation,
                self.config.outputSettings.resolution,
                log_callback=self.log,
            )

            # v1.74：主视频清理独立处理——不论 ffmpeg 成功失败，
            # 用户在 Tab6 选了"移到子文件夹"就该执行（之前卡在 if ok: 里导致失败时不归档）
            # v1.40：处理完一条主视频后，按 mainCleanupMode 归档/删除主视频本身
            if ok:
                # 只对 ffmpeg 实际用到的素材做归档，按 ip.cleanupMode
                # 翻译 GUI key（move/trash/keep）→ 引擎 enum（archive_to_subfolder/delete_to_trash/scan_folder）
                for ip in valid_ips:
                    engine_mode = GUI_TO_CLEANUP_MODE.get(ip.cleanupMode, ip.cleanupMode)
                    if engine_mode in ("scan_folder", "get_video_duration"):
                        continue
                    for up in used_paths:
                        if ip.folder and Path(up).parent == Path(ip.folder):
                            handle_used_material(
                                up, engine_mode,
                                log_callback=self.log,
                            )

            # v1.74：主视频清理（独立于 ffmpeg 成功/失败）——按 mainCleanupMode 归档/删除主视频本身
            main_mode = GUI_TO_CLEANUP_MODE.get(
                self.config.mainCleanupMode, self.config.mainCleanupMode
            )
            self.log(f"[debug engine] mainCleanupMode={self.config.mainCleanupMode!r} → main_mode={main_mode!r}")
            try:
                import datetime as _dt
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] [DBG-ENGINE] ok={ok}, mainCleanupMode={self.config.mainCleanupMode!r} → main_mode={main_mode!r}, main_path={main_path!r}\n")
            except Exception:
                pass
            # v1.78：抛弃 v1.49 中转（先搬 archive_dir 顶层再搬 used/）+ v1.74.2 兜底逻辑。
            # 直接把 archive_dir 传给 handle_used_material，让它根据 main_path 是否是 tmp 决定目标目录。
            if main_mode not in ("scan_folder", "get_video_duration"):
                archive_dir = getattr(self.config, "mainArchiveDir", "") or getattr(self, "_ai_archive_dir", None)
                try:
                    import datetime as _dt
                    ts = _dt.datetime.now().strftime("%H:%M:%S")
                    with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                        f.write(f"[{ts}] [DBG-ENGINE] v1.78 handle_used_material call: main_path={main_path!r}, archive_dir={archive_dir!r}, main_mode={main_mode!r}\n")
                except Exception:
                    pass
                ok_cleanup = handle_used_material(
                    main_path, main_mode,
                    subfolder_name="used",
                    log_callback=self.log,
                    archive_dir=archive_dir,
                )
                self.log(f"[debug engine] handle_used_material(main_path, {main_mode!r}, archive_dir={archive_dir!r}) → ok={ok_cleanup}")
                try:
                    import datetime as _dt
                    ts = _dt.datetime.now().strftime("%H:%M:%S")
                    with open("/tmp/kele_debug.log", "a", encoding="utf-8") as f:
                        f.write(f"[{ts}] [DBG-ENGINE] v1.78 handle_used_material result: ok={ok_cleanup}, src={main_path!r}\n")
                except Exception:
                    pass

        self.log(f"\n🏁 全部完成！处理 {len(main_videos)} 条主视频")
        self.progress_cb(len(main_videos), len(main_videos), "完成")
