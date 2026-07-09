"""
ai_clients.py - v1.20 Tab 6 真实 ASR / LLM 客户端

职责：
  ASRClient.transcribe(video_path) -> [{start, end, text}, ...]
  LLMClient.chat_json(prompt)     -> [...]

不依赖 tkinter，能在线程里跑。

v1.20 ASR 升级：
  silencedetect 切段 → 每段单独调 SenseVoiceSmall → 段内按标点切句
  每段时间戳是真实的（不是估算）
"""
import os
import sys
import re
import json
import time
import uuid
import tempfile
import subprocess
import threading
import signal
import urllib.request
import urllib.error


# ============================================================================
# v2.06 ffmpeg 看门狗（独立进程版）
# 解决 v2.04 线程 watchdog 在主进程整体 SIGSTOP 时失效的问题
# ============================================================================
def _spawn_watchdog(ffmpeg_pid: int, label: str):
    """启动独立 watchdog 子进程监控 ffmpeg_pid。

    v2.10.30 关闭 watchdog：返回 None，不再 spawn 子进程。
    原因：watchdog 在 tessus 启动期 macOS 调度延迟场景下太激进，
          即使把阈值调宽到 30s 也会误杀正在启动的 ffmpeg。
          用户决定完全关掉 watchdog。
    恢复方法：把 v2.10.29 备份里的 _spawn_watchdog 函数体恢复即可。
    """
    return None  # v2.10.30 关闭 watchdog


class _FFmpegWatchDog:
    """
    上下文管理器：启动独立 watchdog 子进程监控 ffmpeg。
    主进程被 SIGSTOP 时 watchdog 子进程仍工作（v2.06 核心改进）。

    用法（不变）：
        with _FFmpegWatchDog("extract_audio") as wd:
            proc = subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE)
            wd.set_pid(proc.pid)
            stdout, stderr = proc.communicate(timeout=180)
    """

    def __init__(self, label: str = "ffmpeg"):
        self.label = label
        self._wd_proc = None

    def set_pid(self, pid: int):
        """Popen 启动后调用：启动 watchdog 子进程监控 pid。"""
        if self._wd_proc is None:
            self._wd_proc = _spawn_watchdog(pid, self.label)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # watchdog 子进程独立，父退出不影响；ffmpeg 退出后 watchdog 自退
        pass


# ============================================================================
# ASR
# ============================================================================
class ASRClient:
    """
    硅基流动（或任意 OpenAI-兼容 /audio/transcriptions）ASR 客户端

    用法：
        cfg = {"url": ..., "key": "...", "model": "..."}
        client = ASRClient(**cfg)
        duration = ASRClient.probe_duration(video_path)
        subtitles = client.transcribe_with_duration(video_path, duration)
    """

    def __init__(self, url: str, key: str, model: str, timeout: int = 300):
        if not key:
            raise RuntimeError("ASR Key 未配置（请到「设置」Tab 填入并保存）")
        self.url = url
        self.key = key
        self.model = model
        self.timeout = timeout

    # ---------- 工具：ffprobe 时长 ----------
    @staticmethod
    def probe_duration(video_path: str) -> float:
        """ffprobe 拿视频时长（秒）。失败返回 0.0"""
        try:
            out = subprocess.check_output(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                timeout=10,
            ).decode().strip()
            return float(out)
        except Exception:
            return 0.0

    # ---------- 工具：ffmpeg 抽音频 ----------
    @staticmethod
    def extract_audio(video_path: str, tmp_dir: str = None) -> str:
        """ffmpeg 把视频抽成 16kHz mono mp3，返回临时路径"""
        tmp_dir = tmp_dir or tempfile.gettempdir()
        audio_path = os.path.join(
            tmp_dir,
            f"kele_asr_{int(time.time() * 1000)}_{os.path.basename(video_path)}.mp3",
        )
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-vn", "-acodec", "libmp3lame",
            "-ar", "16000", "-ac", "1",
            audio_path,
        ]
        try:
            with _FFmpegWatchDog("extract_audio") as wd:
                popen = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    start_new_session=True,  # v2.10.32: 脱离父进程 session，避免父进程 SIGSTOP 拖垮 ffmpeg
                )
                wd.set_pid(popen.pid)
                try:
                    stderr_b, _ = popen.communicate(timeout=180)
                except subprocess.TimeoutExpired:
                    popen.kill()
                    popen.wait()
                    raise RuntimeError(f"ffmpeg 抽音频超时：{video_path}")
            returncode = popen.returncode
            stderr_out = stderr_b
        except RuntimeError:
            raise
        if returncode != 0 or not os.path.exists(audio_path) or os.path.getsize(audio_path) < 100:
            err_tail = (stderr_out or b"").decode("utf-8", errors="ignore").splitlines()[-5:]
            raise RuntimeError(f"ffmpeg 抽音频失败：{video_path}\n" + "\n".join(err_tail))
        return audio_path

    # ---------- v1.20 主入口：silencedetect 切段 + 多段识别 ----------
    def transcribe_with_duration(self, video_path: str, duration: float):
        """
        v1.20 流程：
          1) ffmpeg silencedetect 找静音位置
          2) 按静音把视频音频切成 N 段说话段（单段超过 5s 强制切）
          3) 每段单独调 ASR（SenseVoiceSmall 拿到带标点文本）
          4) 段内按标点切句，按字符数比例分配到该段的时间窗口
        每段时间戳是真实的（不再估算）。
        """
        segments = self.detect_speech_segments(video_path)
        self._last_segments = segments  # 给上层看切了几段

        subtitles = []
        for idx, seg in enumerate(segments):
            seg_path = self.cut_audio_segment(
                video_path, seg["start"], seg["end"]
            )
            try:
                raw = self._post_file(seg_path)
            finally:
                try:
                    os.remove(seg_path)
                except Exception:
                    pass

            text = self._extract_text(raw).strip()
            if not text:
                continue

            sentences = split_sentences(text)
            if not sentences:
                sentences = [text]

            seg_span = seg["end"] - seg["start"]
            per = seg_span / len(sentences)
            for i, s in enumerate(sentences):
                subtitles.append({
                    "start": round(seg["start"] + per * i, 2),
                    "end": round(seg["start"] + per * (i + 1), 2),
                    "text": s,
                })
        return subtitles

    # ---------- v1.20 新增：silencedetect 切段 ----------
    @staticmethod
    def detect_speech_segments(
        video_path: str,
        min_silence: float = 0.3,
        silence_threshold_db: int = -30,
        max_segment_len: float = 5.0,
    ):
        """
        用 ffmpeg silencedetect 检测音频中的说话段。
        返回 [{start, end}, ...]，单段超过 max_segment_len 会被强制切。
        """
        try:
            with _FFmpegWatchDog("detect_speech_segments") as wd:
                popen = subprocess.Popen(
                    [
                        "ffmpeg", "-v", "info",
                        "-i", video_path,
                        "-af",
                        f"silencedetect=noise={silence_threshold_db}dB:d={min_silence}",
                        "-f", "null", "-",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,  # v2.10.32: 脱离父进程 session，避免父进程 SIGSTOP 拖垮 ffmpeg
                )
                wd.set_pid(popen.pid)
                try:
                    stderr_b, _ = popen.communicate(timeout=180)
                except subprocess.TimeoutExpired:
                    popen.kill()
                    popen.wait()
                    raise RuntimeError(f"ffmpeg silencedetect 超时：{video_path}")
            stderr = (stderr_b or b"").decode("utf-8", errors="ignore")
        except RuntimeError:
            raise

        stderr = stderr or ""

        # 解析 silence_start / silence_end，逐行扫
        silence_intervals = []  # [[start, end|None], ...]
        for line in stderr.splitlines():
            m_s = re.search(r"silence_start:\s*([-\d.]+)", line)
            m_e = re.search(r"silence_end:\s*([-\d.]+)", line)
            if m_s:
                silence_intervals.append([float(m_s.group(1)), None])
            elif m_e and silence_intervals and silence_intervals[-1][1] is None:
                silence_intervals[-1][1] = float(m_e.group(1))

        duration = ASRClient.probe_duration(video_path)

        # 推算说话段：[0, ss1) [se1, ss2) [se2, ss3) ... [last_se, duration]
        speech = []
        cursor = 0.0
        for ss, se in silence_intervals:
            if ss > cursor + 0.1:
                speech.append({
                    "start": round(cursor, 2),
                    "end": round(ss, 2),
                })
            if se is not None:
                cursor = max(cursor, se)
            else:
                # 末尾还在静音（少见），保持 cursor 不变
                pass

        if duration > 0 and duration > cursor + 0.1:
            speech.append({
                "start": round(cursor, 2),
                "end": round(duration, 2),
            })

        if not speech:
            return [{"start": 0.0, "end": round(duration or 0.0, 2)}]

        # 强制切：单段超过 max_segment_len 就切，避免长段识别质量差
        final = []
        for seg in speech:
            span = seg["end"] - seg["start"]
            if span <= max_segment_len:
                final.append(seg)
                continue
            t = seg["start"]
            while t < seg["end"]:
                t2 = min(t + max_segment_len, seg["end"])
                final.append({"start": round(t, 2), "end": round(t2, 2)})
                t = t2
        return final

    # ---------- v1.20 新增：切音频片段 ----------
    @staticmethod
    def cut_audio_segment(
        video_path: str, start: float, end: float, tmp_dir: str = None,
    ):
        """从视频里切 [start, end] 段音频，转 16kHz mono mp3，返回临时路径"""
        tmp_dir = tmp_dir or tempfile.gettempdir()
        seg_path = os.path.join(
            tmp_dir,
            f"kele_asr_seg_{int(time.time()*1000)}_"
            f"{start:.2f}_{end:.2f}.mp3",
        )
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", video_path,
            "-to", f"{end - start:.3f}",
            "-vn", "-acodec", "libmp3lame",
            "-ar", "16000", "-ac", "1",
            seg_path,
        ]
        try:
            with _FFmpegWatchDog("cut_audio_segment") as wd:
                popen = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    start_new_session=True,  # v2.10.32: 脱离父进程 session，避免父进程 SIGSTOP 拖垮 ffmpeg
                )
                wd.set_pid(popen.pid)
                try:
                    stderr_b, _ = popen.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    popen.kill()
                    popen.wait()
                    raise RuntimeError(f"ffmpeg 切音频段超时：{start}-{end}")
            returncode = popen.returncode
            stderr_out = stderr_b
        except RuntimeError:
            raise
        if returncode != 0 or not os.path.exists(seg_path) or os.path.getsize(seg_path) < 100:
            err_tail = (stderr_out or b"").decode("utf-8", errors="ignore").splitlines()[-5:]
            raise RuntimeError(
                f"ffmpeg 切音频段失败：{start}-{end}\n" + "\n".join(err_tail)
            )
        return seg_path

    # ---------- v1.20 新增：从 ASR 响应抠文本 ----------
    @staticmethod
    def _extract_text(result) -> str:
        """从 verbose_json / 纯文本响应里抠出拼接好的文本"""
        if isinstance(result, dict):
            segs = result.get("segments")
            if isinstance(segs, list) and segs:
                return " ".join(
                    (s.get("text") or "").strip()
                    for s in segs if s.get("text")
                ).strip()
            return (result.get("text") or "").strip()
        if isinstance(result, str):
            return result.strip()
        return ""

    # ---------- 底层 POST ----------
    def _post_file(self, audio_path: str) -> dict:
        boundary = "----KeleBoundary" + uuid.uuid4().hex[:16]
        with open(audio_path, "rb") as f:
            audio_data = f.read()

        parts = [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="model"\r\n\r\n',
            self.model.encode() + b"\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="response_format"\r\n\r\n',
            b"verbose_json\r\n",
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="audio.mp3"\r\n',
            b"Content-Type: audio/mpeg\r\n\r\n",
            audio_data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        body = b"".join(parts)

        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")[:500]
            raise RuntimeError(f"ASR HTTP {e.code}：{err_body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"ASR 连接失败：{e.reason}")

        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise RuntimeError(f"ASR 响应不是 JSON（前 200 字）：{payload[:200]}")

    # ---------- 解析 ----------
    @staticmethod
    def parse_response(result: dict, duration: float):
        """
        统一吐 [{start, end, text}, ...]
        支持：
          - Whisper verbose_json（带 segments，每段带 start/end/text）
          - 纯文本（如 TeleSpeechASR），按标点切句并按视频时长均分

        v1.19：如果 ASR 只返回 1 段且覆盖大部分时长（说明 ASR 没按静音切句），
        fallback 按句号/标点把这段 text 再切细，时间戳按子句均分。
        解决 TeleSpeechASR 对连续说话视频只返回 1 段的问题。
        """
        subtitles = []

        # Case 1：whisper verbose_json (有 segments)
        if isinstance(result, dict) and isinstance(result.get("segments"), list) and result["segments"]:
            for seg in result["segments"]:
                text = (seg.get("text") or "").strip()
                if not text:
                    continue
                try:
                    start = float(seg.get("start", 0))
                    end = float(seg.get("end", start + 1))
                except (TypeError, ValueError):
                    continue
                if start < 0:
                    start = 0
                if duration > 0 and end > duration:
                    end = duration
                if end <= start:
                    end = start + 0.5
                subtitles.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "text": text,
                })

            # v1.19 fallback：只有 1 段 + 覆盖 ≥70% 时长 → 按标点再切
            if (len(subtitles) == 1 and duration > 0
                    and subtitles[0]["end"] - subtitles[0]["start"] >= duration * 0.7):
                raw_text = subtitles[0]["text"]
                # 直接用增强版 split_sentences（含逗号二级切，threshold=20字）
                sentences = split_sentences(raw_text)
                if len(sentences) > 1:
                    seg = subtitles[0]
                    seg_span = seg["end"] - seg["start"]
                    per = seg_span / len(sentences)
                    subtitles = [
                        {
                            "start": round(seg["start"] + per * i, 2),
                            "end": round(seg["start"] + per * (i + 1), 2),
                            "text": s,
                        }
                        for i, s in enumerate(sentences)
                    ]
            return subtitles

        # Case 2：纯文本（按句号切，时间均分）
        if isinstance(result, dict):
            text = (result.get("text") or "").strip()
        elif isinstance(result, str):
            text = result.strip()
        else:
            text = ""
        if not text:
            return []

        sentences = split_sentences(text)
        if not sentences:
            sentences = [text]

        per = (duration if duration > 0 else 3.0 * len(sentences)) / len(sentences)
        for i, s in enumerate(sentences):
            subtitles.append({
                "start": round(per * i, 2),
                "end": round(per * (i + 1), 2),
                "text": s,
            })
        return subtitles


def split_sentences(text: str, max_len: int = 15):
    """
    按中英文标点切句（v1.19 增强）：
      1) 先按句末标点 [。！？.!?;；] 切
      2) 如果结果里某段 >20 字，按逗号 [，,；;] 再切
      3) 如果整段 >20 字仍只有 1 段，直接按逗号切
      4) 后处理：超过 max_len（默认 15 字）的子段强制按字符均分，
         解决 ASR 把多段连读（"601000套是全款不是首付"）的硬块
    返回的句子尽量短，让时间戳更细。
    """
    if not text:
        return []

    def _by_punct(s: str, pat: str):
        parts = re.split(pat, s)
        return [p.strip() for p in parts if p and p.strip()]

    def _split_by_len(s: str):
        """按 max_len 字符均分"""
        if len(s) <= max_len:
            return [s]
        n = (len(s) + max_len - 1) // max_len  # 向上取整
        chunk_size = (len(s) + n - 1) // n
        out = []
        for i in range(n):
            chunk = s[i*chunk_size:(i+1)*chunk_size]
            if chunk:
                out.append(chunk)
        return out

    # 第一遍：句末标点
    parts = re.findall(r"[^。！？.!?;；\n]+[。！？.!?;；]?", text)
    out = [p.strip() for p in parts if p and p.strip()]
    if out and len(out) > 1:
        refined = []
        for s in out:
            if len(s) > 20:
                refined.extend(_by_punct(s, r"[，,；;]"))
            else:
                refined.append(s)
        if refined:
            # v1.19 后处理：超过 max_len 的强制均分
            final = []
            for s in refined:
                final.extend(_split_by_len(s))
            return final

    # 第二遍：按逗号切
    parts2 = re.split(r"[，,；;]\s*", text)
    out2 = [p.strip() for p in parts2 if p and p.strip()]
    if len(out2) > 1:
        final = []
        for s in out2:
            final.extend(_split_by_len(s))
        return final

    # 第三遍：按换行/空格切
    parts3 = re.split(r"[\n\s]+\s*", text)
    out3 = [p.strip() for p in parts3 if p and p.strip()]
    if len(out3) > 1:
        return out3

    # 实在没法切，原样返回
    return [text.strip()] if text.strip() else []


# ============================================================================
# LLM
# ============================================================================
class LLMClient:
    """
    硅基流动（或任意 OpenAI-兼容 /chat/completions）LLM 客户端

    用法：
        client = LLMClient(url=..., key=..., model=..., temperature=0.3, max_tokens=8192)
        data = client.chat_json(prompt)
    """

    def __init__(self, url: str, key: str, model: str,
                 temperature: float = 0.3, max_tokens: int = 8192, timeout: int = 120):
        if not key:
            raise RuntimeError("LLM Key 未配置（请到「设置」Tab 填入并保存）")
        self.url = url
        self.key = key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    def chat_json(self, prompt: str):
        """
        调用 LLM，返回 list[dict]。
        v1.18：
          - 兼容 SSE 流式响应（合并 delta.content）
          - 不强加 response_format（本地模型可能不支持，反而乱回答）
          - 响应解析失败时把原始内容存到 /tmp 方便诊断
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")[:500]
            raise RuntimeError(f"LLM HTTP {e.code}：{err_body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM 连接失败：{e.reason}")

        # v1.18：检测 SSE 流（本地 vllm 之类默认走流式）
        content = ""
        if raw.lstrip().startswith("data:"):
            content = self._parse_sse(raw)
            if not content:
                # SSE 流但解析不到 content
                debug_path = os.path.join(tempfile.gettempdir(), "kele_llm_raw.txt")
                try:
                    with open(debug_path, "w", encoding="utf-8") as f:
                        f.write(raw)
                except Exception:
                    pass
                raise RuntimeError(
                    f"LLM SSE 流解析不到 content（前 300 字）：{raw[:300]}\n"
                    f"完整内容已存到：{debug_path}"
                )
        else:
            # 普通 JSON 响应
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                debug_path = os.path.join(tempfile.gettempdir(), "kele_llm_raw.txt")
                try:
                    with open(debug_path, "w", encoding="utf-8") as f:
                        f.write(raw)
                except Exception:
                    pass
                raise RuntimeError(
                    f"LLM 响应不是 JSON（前 300 字）：{raw[:300]}\n"
                    f"完整内容已存到：{debug_path}"
                )

            try:
                content = result["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(f"LLM 响应格式异常：{str(result)[:500]}")

        return parse_llm_json(content)

    @staticmethod
    def _parse_sse(raw: str) -> str:
        """从 SSE 流（data: {...}\\n\\n）合并所有 delta.content"""
        full = ""
        for line in raw.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                if payload == "[DONE]":
                    break
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            chunk = delta.get("content") or ""
            if chunk:
                full += chunk
        return full


def parse_llm_json(content: str):
    """
    从 LLM 输出解析 JSON 数组（v1.17 加强版）。
    兼容：
      - 纯 JSON 数组
      - markdown ```json ... ``` 包裹
      - 顶层 dict 含 insertions/results/data/items 字段
      - 文本里夹着 {...} 或 [...]
    """
    content = content.strip()
    if not content:
        raise RuntimeError("LLM 输出为空")

    # 去掉 markdown 代码块包裹
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl > 0:
            content = content[first_nl + 1:]
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3].rstrip()

    # 直接 parse
    try:
        data = json.loads(content)
        return _extract_insertions(data)
    except json.JSONDecodeError:
        pass

    # v1.42：LLM 偶尔漏掉开头的 [（输出形如 {"start":...},{"start":...}]）
    # 自动补 [ 让 json.loads 成功
    patched = content.strip()
    if patched and patched[0] != "[":
        # 先确认能找到 { 表示这是一个对象流
        if patched.startswith("{"):
            patched = "[" + patched
            # 如果缺 ] 顺便补
            if not patched.rstrip().endswith("]"):
                patched = patched.rstrip().rstrip(",") + "]"
            try:
                data = json.loads(patched)
                return _extract_insertions(data)
            except json.JSONDecodeError:
                pass

    # 提取第一段 [...] JSON（非贪婪）
    m = re.search(r"\[\s*\{.*?\}\s*(?:,\s*\{.*?\}\s*)*\]", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # 提取第一段 {...} JSON
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return _extract_insertions(data)
        except json.JSONDecodeError:
            pass

    # 最终兜底：保存到 debug 文件
    debug_path = os.path.join(tempfile.gettempdir(), "kele_llm_content.txt")
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass
    raise RuntimeError(
        f"LLM 输出无法解析为 JSON 数组（前 300 字）：{content[:300]}\n"
        f"完整内容已存到：{debug_path}"
    )


def _extract_insertions(data):
    """从 LLM 返回的 JSON 里抠出插入点数组"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # 常见的几种字段名
        for key in ("insertions", "results", "data", "items", "points"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        # 单条 dict 也接受
        if any(k in data for k in ("start", "folder_label", "end")):
            return [data]
    raise RuntimeError(f"LLM JSON 不是数组或含 insertions 字段：{str(data)[:200]}")


# ============================================================================
# 测试时直接跑
# ============================================================================
if __name__ == "__main__":
    print("ai_clients module — 直接跑没意义，请在 app 里调")
