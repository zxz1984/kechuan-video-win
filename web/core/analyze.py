"""
Tab 6 (AI 智能) 业务核心：纯函数版

不依赖 Tkinter，可以命令行调用 / 给 Flask 用。
原版逻辑来自 ai_tab.py::_llm_real + _asr_real，本文件做最小改动以便独立运行。

用法（命令行）：
    python -m web.core.analyze \\
        --video /path/to/video.mp4 \\
        --strategy sandwich \\
        --industry 南宁房产 \\
        --preset-mode 3 \\
        --materials "素材A:5s:0,素材B:8s:0" \\
        --output /path/to/plan.json

用法（import）：
    from web.core.analyze import analyze_video
    result = analyze_video(
        "/path/to/video.mp4",
        materials=[{"label": "素材A", "seconds": 5, "required": False}],
        insert_strategy="sandwich",
        industry="南宁房产",
        preset_mode="3",
    )
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import yaml

# 让本文件能 import ai_clients.py（项目内）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from ai_clients import ASRClient, LLMClient  # noqa: E402

# ===== 默认值（与 main.py LLM_/ASR_ 一致，避免 import main 触发 Tkinter）=====
LLM_DEFAULT_URL = "https://api.siliconflow.cn/v1/chat/completions"
LLM_DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
ASR_DEFAULT_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"

LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 8192

CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# ===== 策略 prompt 块（与 ai_tab.py 严格一致）=====
_STRATEGY_BLOCKS = {
    "free": (
        "Freely decide insertion points based on subtitle content and material relevance. "
        "Balance small/large insertions to keep the video dynamic."
    ),
    "sandwich": (
        "⚠️ STRICT SANDWICH STRUCTURE — you MUST follow this exactly:\n"
        "  - HARD CONSTRAINT 1: NO insertion point with start < 3.0s (head/host must remain on screen)\n"
        "  - HARD CONSTRAINT 2: NO insertion point whose end is within the last 3.0s of the video (tail/host must remain)\n"
        "  - MIDDLE ONLY (between 3.0s and duration-3.0s): use 'large' mode aggressively (5-15 seconds per insertion)\n"
        "  - Total video duration ≈ {duration:.1f}s. Insert 3-8 points focused on the middle 60-70% of the timeline.\n"
        "  - Repeat: ZERO insertions in head (first 3.0s) and tail (last 3.0s). Violations will be auto-rejected."
    ),
    "two_anchor": (
        "⚠️ STRICT TWO-ANCHOR STRUCTURE — you MUST follow this exactly:\n"
        "  - HARD CONSTRAINT 1: NO insertion point with start < 3.0s (head/host must remain on screen)\n"
        "  - HARD CONSTRAINT 2: NO insertion point whose end is within the last 3.0s of the video (tail/host must remain)\n"
        "  - MIDDLE ONLY (between 3.0s and duration-3.0s): insert materials to FILL the material's natural duration "
        "(don't waste good material by only using 1-2 seconds)\n"
        "  - Total video duration ≈ {duration:.1f}s. Each insertion should match its material's full length "
        "(e.g. 5s material → 5s insertion, 10s material → 10s insertion).\n"
        "  - Repeat: ZERO insertions in head (first 3.0s) and tail (last 3.0s). Violations will be auto-rejected."
    ),
}

_MODE_INTRO = {
    "1": "每个素材文件夹插入 1 个，按字幕序号分布",
    "2": "必插文件夹每个必出 1 个；其他可选素材由你决定是否插入及插入多少个",
    "3": "你完全决定插入多少个、插入到哪里",
}


def load_config(path: Optional[Path] = None) -> dict:
    """读 config.yaml，返回 dict。文件不存在时返回空 dict。"""
    p = path or CONFIG_PATH
    if not p.exists():
        return {}
    with open(p) as f:
        return yaml.safe_load(f) or {}


def _build_prompt(
    subtitles: list,
    materials: list,
    preset_mode: str,
    strategy: str,
    industry: str,
    duration: float,
) -> str:
    sub_lines = [
        f"[{i}] {s['start']:.1f}s-{s['end']:.1f}s  {s['text']}"
        for i, s in enumerate(subtitles)
    ]
    sub_text = "\n".join(sub_lines) if sub_lines else "（无字幕）"

    mat_lines = [
        f"- {m['label']}（约 {m.get('seconds', 5)} 秒，{'必插' if m.get('required') else '可选'}）"
        for m in materials
    ]
    mat_text = "\n".join(mat_lines) if mat_lines else "（无素材）"

    mode_intro = _MODE_INTRO.get(preset_mode, "由你全权决定")
    strategy_block = _STRATEGY_BLOCKS.get(strategy, _STRATEGY_BLOCKS["free"])

    if strategy in ("sandwich", "two_anchor"):
        strategy_block = strategy_block.format(duration=duration)

    industry_context = ""
    if industry and industry.strip() and industry.strip() != "通用":
        industry_context = (
            f"\nVideo industry: {industry}\n"
            f"Use this to inform your decisions: subtitle topics, keyword interpretation, "
            f"and which material is most relevant for each insertion point.\n"
        )

    return f"""You are a short video editor for a Chinese talking-head (口播) video channel. Decide where to insert materials based on subtitles.{industry_context}
Material-mode: {preset_mode} - {mode_intro}

Insertion strategy: {strategy}
{strategy_block}

Subtitles (in order, [{len(subtitles)}] segments, total {duration:.1f}s):
{sub_text}

Material folders (mark "required" means MUST insert; "X秒" is material natural duration):
{mat_text}

Output ONLY a JSON array. No markdown, no explanation, no prefix.
Each element must have exactly these fields:
- "start": number (seconds, align to a subtitle's start time)
- "end": number (seconds, align to a subtitle's end time, or later if "large" mode)
- "folder_label": string (must be one of the labels above, no invented names)
- "mode": "small" (1 subtitle segment, 1 material clip) or "large" (spans 1-3 segments, longer material use)
- "reason": string (max 10 chars, Chinese or English, like "开头铺垫" / "产品演示" / "closing")

Constraints:
- start and end must come from the subtitles above (or 'end' may extend up to 3× the next subtitle's end time when mode="large")
- folder_label must be exactly one of the listed labels above
- output 1-{len(materials) or 1} insertion points
- reply starts with [ and ends with ]
"""


def _enforce_anchor_window(insertions: list, duration: float, log=print) -> list:
    """客户端兜底校验：sandwich / two_anchor 强制剔除头尾 3 秒内的插入点"""
    if duration <= 0:
        return insertions
    head_cutoff = 3.0
    tail_cutoff = duration - 3.0
    before = len(insertions)
    kept = []
    dropped = []
    for ins in insertions:
        try:
            s = float(ins.get("start", 0))
            e = float(ins.get("end", 0))
        except (TypeError, ValueError):
            dropped.append((ins, "start/end 不是数字"))
            continue
        if s < head_cutoff:
            dropped.append((ins, f"start={s:.1f}s < 3s（头）"))
            continue
        if e > tail_cutoff:
            dropped.append((ins, f"end={e:.1f}s > {tail_cutoff:.1f}s（尾）"))
            continue
        kept.append(ins)
    if dropped:
        log(f"  ⚠️ 策略校验：剔除 {len(dropped)} 个违反头尾约束的插入点")
        for ins, reason in dropped:
            log(f"     · 剔除 [{ins.get('start', '?')}-{ins.get('end', '?')}] {reason}")
    else:
        log(f"  ✅ 策略校验：{before} 个插入点全部符合头尾约束")
    return kept


def analyze_video(
    video_path: str,
    *,
    materials: Optional[list] = None,
    preset_mode: str = "3",
    insert_strategy: str = "free",
    industry: str = "通用",
    config: Optional[dict] = None,
    log=print,
) -> dict:
    """
    一站式分析：上传视频 → ASR → LLM → 客户端校验 → 返 JSON

    Args:
        video_path: 主视频文件路径
        materials: 素材列表 [{label, seconds, required}, ...]；None 表示无素材
        preset_mode: "1" / "2" / "3"
        insert_strategy: "free" / "sandwich" / "two_anchor"
        industry: 行业关键词，"通用" 表示不传上下文
        config: 配置 dict（含 asr/llm 节），None 则从 config.yaml 读
        log: 日志回调，默认 print

    Returns:
        dict: {video, duration, strategy, industry, subtitles, insertions}
    """
    materials = materials or []
    cfg = config if config is not None else load_config()
    asr_cfg = cfg.get("asr", {}) or {}
    llm_cfg = cfg.get("llm", {}) or {}

    if not Path(video_path).exists():
        raise FileNotFoundError(video_path)

    # === Step 1: ASR ===
    log(f"[1/3] ASR: {video_path}")
    asr_url = (asr_cfg.get("url") or ASR_DEFAULT_URL).strip()
    asr_key = (asr_cfg.get("key") or "").strip()
    asr_model = (asr_cfg.get("model") or ASR_DEFAULT_MODEL).strip()
    if not asr_key:
        raise RuntimeError("ASR Key 未配置（config.yaml 里 asr.key 为空）")

    duration = ASRClient.probe_duration(video_path)
    log(f"  📐 时长 {duration:.1f}s，开始 ASR ...")
    asr_client = ASRClient(url=asr_url, key=asr_key, model=asr_model)
    subtitles = asr_client.transcribe_with_duration(video_path, duration)
    log(f"  🎙 {len(subtitles)} 句字幕")

    insertions = []
    if subtitles:
        # === Step 2: LLM ===
        log(f"[2/3] LLM: 拼 prompt")
        llm_url = (llm_cfg.get("url") or LLM_DEFAULT_URL).strip()
        llm_key = (llm_cfg.get("key") or "").strip()
        llm_model = (llm_cfg.get("model") or LLM_DEFAULT_MODEL).strip()
        if not llm_key:
            raise RuntimeError("LLM Key 未配置（config.yaml 里 llm.key 为空）")

        llm_client = LLMClient(
            url=llm_url, key=llm_key, model=llm_model,
            temperature=LLM_TEMPERATURE, max_tokens=LLM_MAX_TOKENS,
        )
        prompt = _build_prompt(subtitles, materials, preset_mode, insert_strategy, industry, duration)
        log(f"  🤖 调 LLM: {llm_model} @ {llm_url}，{len(subtitles)} 字幕 + {len(materials)} 素材")

        insertions = llm_client.chat_json(prompt)

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

        # === Step 3: 客户端兜底校验 ===
        log(f"[3/3] 校验")
        if insert_strategy in ("sandwich", "two_anchor"):
            insertions = _enforce_anchor_window(insertions, duration, log)
        log(f"  ✅ 完成: {len(insertions)} 个插入点")
    else:
        log("[2/3] LLM 跳过 (无字幕)")
        log("[3/3] 校验 跳过")

    return {
        "video": video_path,
        "duration": duration,
        "preset_mode": preset_mode,
        "strategy": insert_strategy,
        "industry": industry,
        "subtitles": subtitles,
        "insertions": insertions,
    }


# ===== 命令行入口 =====

def _parse_materials(s: Optional[str]) -> list:
    """解析 --materials 字符串
    格式：'素材A:5:0,素材B:8:1'
    每个素材：label:seconds:required(0/1)
    """
    if not s:
        return []
    out = []
    for piece in s.split(","):
        parts = piece.strip().split(":")
        if len(parts) < 3:
            continue
        label = parts[0].strip() or "素材"
        try:
            seconds = int(parts[1])
        except ValueError:
            seconds = 5
        try:
            required = bool(int(parts[2]))
        except ValueError:
            required = False
        out.append({"label": label, "seconds": seconds, "required": required})
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="可乐 Tab 6 AI 分析命令行")
    parser.add_argument("--video", required=True, help="主视频文件路径")
    parser.add_argument("--preset-mode", default="3", choices=["1", "2", "3"],
                        help="预设模式 (默认 3=LLM 完全决定)")
    parser.add_argument("--strategy", default="free",
                        choices=["free", "sandwich", "two_anchor"],
                        help="插入策略 (默认 free)")
    parser.add_argument("--industry", default="通用", help="视频行业关键词，例如 南宁房产")
    parser.add_argument("--materials", default="",
                        help="素材列表: 'label:seconds:required,...' 例如 '素材A:5:0,素材B:8:1'")
    parser.add_argument("--output", default="", help="输出 JSON 文件路径（默认打印到 stdout）")
    parser.add_argument("--config", default="", help="config.yaml 路径（默认项目根目录的 config.yaml）")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config)) if args.config else None
    materials = _parse_materials(args.materials)

    result = analyze_video(
        args.video,
        materials=materials,
        preset_mode=args.preset_mode,
        insert_strategy=args.strategy,
        industry=args.industry,
        config=cfg,
    )

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"\n✅ 已写入 {args.output}（{len(result['insertions'])} 个插入点）")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
