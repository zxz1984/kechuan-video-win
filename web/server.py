"""
可乐 AI 工作台 网页版 - Flask 入口（v1.31 一比一复刻 .app UI）
监听 0.0.0.0:8080（端口避开 1.12.50.45:3011 的另一个软件）

路由：
  GET  /                       → 主页（功能导航）
  GET  /task                   → Tab 1 任务（一比一复刻）
  GET  /persona                → Tab 2 人设管理
  GET  /settings               → Tab 3 设置（ASR/LLM/API Key）
  GET  /product                → Tab 4 成品
  GET  /merge                  → Tab 5 混剪/插段
  GET  /ai                     → Tab 6 🤖 AI 智能（一比一复刻）
  GET  /analyze                → Tab 6 旧版 AI 上传页（功能等价于 /ai，但更紧凑表单）
  POST /analyze                → 提交 AI 分析任务
  GET  /analyze/status/<tid>   → 轮询分析状态
  GET  /analyze/result/<tid>   → 下载 plan.json
  GET  /generate               → Tab 1-4 视频生成上传页
  POST /generate               → 提交生成任务（直接 import KoboEngine，零改动）
  GET  /generate/status/<tid>  → 轮询生成状态
  GET  /generate/result/<tid>  → 下载成品 mp4
  GET  /health                 → 健康检查
  GET  /me                     → 当前用户（v1.29 永远返回 guest）

模板：
  web/templates/base.html      → 顶 nav + 全局 CSS
  web/templates/index.html      → 主页（6 个功能卡片）
  web/templates/task.html       → Tab 1 任务
  web/templates/persona.html    → Tab 2 人设管理
  web/templates/settings.html   → Tab 3 设置
  web/templates/product.html    → Tab 4 成品
  web/templates/merge.html      → Tab 5 混剪/插段
  web/templates/ai.html         → Tab 6 AI 智能（完整 5 段 + 审核面板）
  web/templates/analyze.html    → /analyze（旧版紧凑表单）
  web/templates/generate.html   → /generate（视频生成表单）

用法：
    python3.13 -m web.server
或：
    gunicorn -w 2 -b 0.0.0.0:8080 web.server:app
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template, Response

# 让 web.server 能 import web.core.* 和 .app 的业务代码
import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from web.core.analyze import analyze_video        # noqa: E402
from kobo_engine import KoboEngine                # noqa: E402（直接复用 .app 跑通代码）
from web.core.auth import (  # noqa: E402
    init_db, current_user, login_required,
    task_create, task_get, task_append_log,
    task_finish, task_count_active, task_cleanup,
)

# ===== 配置 =====
JOB_DIR = Path(os.environ.get("WEB_JOB_DIR", "/tmp/web_jobs"))
JOB_DIR.mkdir(parents=True, exist_ok=True)
TASK_TTL_MIN = int(os.environ.get("WEB_TASK_TTL_MIN", "10"))
PORT = int(os.environ.get("WEB_PORT", "8080"))
HOST = os.environ.get("WEB_HOST", "0.0.0.0")
APP_VERSION = "1.31"

app = Flask(__name__)

# 启动时建用户/预设/任务表
init_db()


# ===== 任务清理线程 =====
def _cleanup_loop():
    while True:
        time.sleep(60)
        try:
            deleted = task_cleanup(ttl_seconds=TASK_TTL_MIN * 60)
            if deleted:
                print(f"[cleanup] removed {deleted} old tasks from SQLite")
            cutoff = time.time() - TASK_TTL_MIN * 60
            for d in JOB_DIR.iterdir():
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    try:
                        shutil.rmtree(d, ignore_errors=True)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[cleanup] error: {e}")

threading.Thread(target=_cleanup_loop, daemon=True).start()


# ============================================================================
# 一比一复刻 .app 6 个 Tab 的静态页面（先 UI 后接逻辑）
# ============================================================================
@app.route("/")
def index():
    return render_template("index.html", active="home")


@app.route("/task")
def task_page():
    return render_template("task.html", active="task")


@app.route("/persona")
def persona_page():
    return render_template("persona.html", active="persona")


@app.route("/settings")
def settings_page():
    return render_template("settings.html", active="settings")


@app.route("/product")
def product_page():
    return render_template("product.html", active="product")


@app.route("/merge")
def merge_page():
    return render_template("merge.html", active="merge")


@app.route("/ai")
def ai_page():
    return render_template("ai.html", active="ai")


# ============================================================================
# 工具接口
# ============================================================================
@app.route("/me")
def me():
    return jsonify(current_user())


@app.route("/health")
def health():
    return jsonify({"status": "ok", "tasks": task_count_active(), "time": datetime.now().isoformat(), "version": APP_VERSION})


@app.route("/speedtest/<int:mb_size>")
def speedtest(mb_size: int):
    """临时测速端点：返回 N MB 零字节，用于测服务器→客户端下行带宽。
    例子：curl -o /dev/null http://host:8080/speedtest/50
    """
    if mb_size < 1 or mb_size > 200:
        return jsonify({"error": "mb_size 范围 1-200"}), 400
    chunk = b"\0" * (1024 * 1024)  # 1 MB
    def gen():
        for _ in range(mb_size):
            yield chunk
    return Response(gen(), mimetype="application/octet-stream")


# ============================================================================
# Tab 6: AI 智能分析（/analyze/*）—— 紧凑表单版（已有功能完整）
# ============================================================================
@app.route("/analyze")
def analyze_page():
    return render_template("analyze.html", active="ai")


@login_required
@app.route("/analyze", methods=["POST"])
def analyze_submit():
    if "video" not in request.files:
        return jsonify({"error": "no video file"}), 400

    video_file = request.files["video"]
    if not video_file.filename:
        return jsonify({"error": "empty filename"}), 400

    strategy = request.form.get("strategy", "free")
    industry = request.form.get("industry", "通用").strip() or "通用"
    preset_mode = request.form.get("preset_mode", "3")
    materials_str = request.form.get("materials", "")

    materials = []
    for piece in materials_str.split(","):
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
        materials.append({"label": label, "seconds": seconds, "required": required})

    task_id = uuid.uuid4().hex[:12]
    job_dir = JOB_DIR / task_id
    job_dir.mkdir(parents=True, exist_ok=True)

    safe_name = Path(video_file.filename).name
    video_path = job_dir / safe_name
    video_file.save(str(video_path))

    task_create(task_id, "analyze", [f"📁 已接收视频: {safe_name}"])

    def _run():
        try:
            result = analyze_video(
                str(video_path),
                materials=materials,
                preset_mode=preset_mode,
                insert_strategy=strategy,
                industry=industry,
                log=lambda m: task_append_log(task_id, m),
            )
            json_path = job_dir / "plan.json"
            json_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            task_append_log(task_id, f"💾 已保存 plan.json（{len(result['insertions'])} 个插入点）")
            task_finish(task_id, "done", result_path=str(json_path))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            task_append_log(task_id, f"❌ {err}")
            task_finish(task_id, "failed", error=err)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/analyze/status/<task_id>")
def analyze_status(task_id):
    task = task_get(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    return jsonify({
        "status": task["status"],
        "log": task.get("log", []),
        "error": task.get("error"),
    })


@app.route("/analyze/result/<task_id>")
def analyze_result(task_id):
    task = task_get(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    if task["status"] != "done":
        return jsonify({"error": "task not done, status=" + task["status"]}), 400
    result_path = Path(task["result_path"])
    if not result_path.exists():
        return jsonify({"error": "result file missing"}), 500
    return send_file(
        str(result_path),
        as_attachment=True,
        download_name=f"plan_{task_id}.json",
    )


# ============================================================================
# Tab 1-4: 视频生成（/generate/*）—— 直接 import KoboEngine，零业务改动
# ============================================================================
@app.route("/generate")
def generate_page():
    return render_template("generate.html", active="task")


@login_required
@app.route("/generate", methods=["POST"])
def generate_submit():
    if "image" not in request.files or "audio" not in request.files:
        return jsonify({"error": "需要上传图片和声音"}), 400

    image_file = request.files["image"]
    audio_file = request.files["audio"]
    if not image_file.filename or not audio_file.filename:
        return jsonify({"error": "文件名为空"}), 400

    script = (request.form.get("script") or "").strip()
    if not script:
        return jsonify({"error": "文案不能为空"}), 400

    action_prompt = (request.form.get("action_prompt") or "").strip()
    persona = (request.form.get("persona") or "default").strip()

    task_id = uuid.uuid4().hex[:12]
    job_dir = JOB_DIR / task_id
    job_dir.mkdir(parents=True, exist_ok=True)

    image_ext = Path(image_file.filename).suffix or ".jpg"
    audio_ext = Path(audio_file.filename).suffix or ".mp3"
    image_path = job_dir / f"image{image_ext}"
    audio_path = job_dir / f"audio{audio_ext}"
    image_file.save(str(image_path))
    audio_file.save(str(audio_path))

    task_create(task_id, "generate", [
        f"📁 已接收素材: {image_path.name} + {audio_path.name}",
        f"🎭 人设: {persona}",
        f"📝 文案: {len(script)}字",
    ])

    def _run():
        try:
            engine = KoboEngine(
                config_path=str(PROJECT_ROOT / "config.yaml"),
                log_callback=lambda m: task_append_log(task_id, m),
            )
            result = engine.run(
                persona=persona,
                script=script,
                image_path=str(image_path),
                sample_audio_path=str(audio_path),
                output_dir=str(job_dir),
                action_prompt=action_prompt,
                progress_callback=lambda pct, msg: task_append_log(task_id, f"[{pct}%] {msg}"),
            )
            if result.get("status") != "SUCCESS":
                err = result.get("error", "未知失败")
                task_append_log(task_id, f"❌ {err}")
                task_finish(task_id, "failed", error=err)
                return
            output_path = Path(result["output"])
            if not output_path.exists():
                task_append_log(task_id, f"❌ 成品文件不存在: {result['output']}")
                task_finish(task_id, "failed", error="output file missing")
                return
            task_append_log(task_id, f"💾 成品: {output_path.name} ({output_path.stat().st_size // 1024} KB)")
            task_finish(task_id, "done", result_path=str(output_path))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            task_append_log(task_id, f"❌ {err}")
            task_finish(task_id, "failed", error=err)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/generate/status/<task_id>")
def generate_status(task_id):
    task = task_get(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    return jsonify({
        "status": task["status"],
        "log": task.get("log", []),
        "error": task.get("error"),
    })


@app.route("/generate/result/<task_id>")
def generate_result(task_id):
    task = task_get(task_id)
    if not task:
        return jsonify({"error": "task not found"}), 404
    if task["status"] != "done":
        return jsonify({"error": "task not done, status=" + task["status"]}), 400
    result_path = Path(task["result_path"])
    if not result_path.exists():
        return jsonify({"error": "result file missing"}), 500
    return send_file(
        str(result_path),
        as_attachment=True,
        download_name=f"{task_id}_video.mp4",
        mimetype="video/mp4",
    )


# ===== 启动 =====
if __name__ == "__main__":
    print(f"🚀 可乐 AI 工作台 v{APP_VERSION} 启动")
    print(f"   监听: {HOST}:{PORT}")
    print(f"   任务目录: {JOB_DIR}")
    print(f"   任务 TTL: {TASK_TTL_MIN} 分钟")
    print(f"   当前用户: {current_user()}")
    print(f"   模板目录: {PROJECT_ROOT / 'web' / 'templates'}")
    print(f"   路由列表:")
    for rule in app.url_map.iter_rules():
        print(f"     {rule.methods - {'HEAD', 'OPTIONS'}} {rule.rule}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)