import os
import re
import math
import json
import time
import random
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, FIRST_COMPLETED
from concurrent.futures import wait as futures_wait

import requests
import yaml


class KoboEngine:
    MAX_CONCURRENT = 5
    AUDIO_POLL_INTERVAL = 10
    VIDEO_POLL_INTERVAL = 20
    AUDIO_TIMEOUT = 600
    VIDEO_TIMEOUT = 3000
    MAX_SEGMENT_DURATION = 60
    CHARS_PER_SECOND = 0.21
    MAX_API_RETRIES = 3
    MAX_TASK_RETRIES = 1
    MAX_DOWNLOAD_RETRIES = 3
    RETRY_DELAY = 5

    def _get_tool_path(self, tool_name):
        """获取工具路径（兼容打包环境 + dev 环境 + 系统 PATH）
        v1.72 修复：兼容 ffmpeg.app bundle 风格（bin/ffmpeg/ffmpeg 目录包裹）
        """
        import sys
        import os
        import shutil

        tool_path = tool_name
        if sys.platform == "win32":
            tool_path += ".exe"

        def _find_in_bin(bin_dir):
            if not bin_dir or not os.path.isdir(bin_dir):
                return None
            # 优先 1：bin/tool_name（直接文件）
            direct = os.path.join(bin_dir, tool_path)
            if os.path.isfile(direct):
                return direct
            # 优先 2：bin/tool_name/tool_name（PyInstaller 把整个目录当成 binary 打，目录包裹）
            nested = os.path.join(bin_dir, tool_path, tool_path)
            if os.path.isfile(nested):
                return nested
            return None

        if hasattr(sys, '_MEIPASS'):
            # PyInstaller onedir 模式：.app/Contents/Frameworks/bin/
            meipass_bin = os.path.join(sys._MEIPASS, '..', 'Frameworks', 'bin')
            found = _find_in_bin(meipass_bin)
            if found:
                return found
            # PyInstaller onefile 模式 / Resources/bin
            meipass_direct_bin = os.path.join(sys._MEIPASS, 'bin')
            found = _find_in_bin(meipass_direct_bin)
            if found:
                return found
            # 兜底：sys._MEIPASS/tool_name
            direct_meipass = os.path.join(sys._MEIPASS, tool_path)
            if os.path.isfile(direct_meipass):
                return direct_meipass

        # Mac APP 打包（onedir）：.app/Contents/MacOS/../Frameworks/bin/
        app_bin = os.path.join(os.path.dirname(sys.executable), '..', 'Frameworks', 'bin')
        found = _find_in_bin(app_bin)
        if found:
            return found

        # 项目本地 bin/（开发环境）
        project_bin = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin')
        # 优先 0：临时测试 ffmpeg 软切换（v2.10.16 调试用，存在 .bak_tessus 就用）
        # 注意：只对 ffmpeg 切换，ffprobe 走原路径（tessus 不提供 ffprobe 二进制）
        if tool_path == 'ffmpeg':
            tessus_path = os.path.join(project_bin, 'ffmpeg.bak_tessus')
            if os.path.isfile(tessus_path):
                return tessus_path
        found = _find_in_bin(project_bin)
        if found:
            return found

        # 系统 PATH
        sys_path = shutil.which(tool_path)
        if sys_path:
            return sys_path

        # 兜底返回 tool_name 让 subprocess 自己报错
        return tool_path

    def __init__(self, config_path: str = None, log_callback=None):
        self.log_callback = log_callback
        self.config = {}
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        self._running = False
        self._active_task_ids = []
        self._task_ids_lock = threading.Lock()

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if self.log_callback:
            self.log_callback(line)

    def _get(self, key, default=""):
        keys = key.split(".")
        val = self.config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k, default)
            else:
                return default
            if val is None:
                return default
        return val

    def _get_max_concurrent(self):
        val = self._get("max_concurrent", "5")
        try:
            n = int(val)
            return max(1, min(n, 5))
        except (ValueError, TypeError):
            return 5

    def stop(self):
        self._cancel_all_tasks()
        self._running = False

    def _register_task(self, task_id):
        with self._task_ids_lock:
            if task_id not in self._active_task_ids:
                self._active_task_ids.append(task_id)

    def _unregister_task(self, task_id):
        with self._task_ids_lock:
            if task_id in self._active_task_ids:
                self._active_task_ids.remove(task_id)

    def _cancel_task(self, task_id):
        try:
            url = "https://www.runninghub.cn/task/openapi/cancel"
            headers = {
                "Authorization": f"Bearer {self._get('api.key')}",
                "Content-Type": "application/json",
            }
            payload = {
                "apiKey": self._get('api.key'),
                "taskId": task_id,
            }
            self._log(f"正在取消任务 {task_id}...")
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                self._log(f"已取消任务 {task_id}")
            else:
                self._log(f"取消任务 {task_id} 返回: {data.get('msg', '未知')}")
        except Exception as e:
            self._log(f"取消任务 {task_id} 异常: {e}")

    def _cancel_all_tasks(self):
        with self._task_ids_lock:
            task_ids = list(self._active_task_ids)
            self._active_task_ids.clear()
        if task_ids:
            self._log(f"正在取消 {len(task_ids)} 个运行中的任务...")
            for tid in task_ids:
                self._cancel_task(tid)

    def _retry_request(self, func, max_retries=None, label="请求", **kwargs):
        if max_retries is None:
            max_retries = self.MAX_API_RETRIES
        last_error = None
        for attempt in range(1, max_retries + 1):
            if not self._running:
                raise RuntimeError("用户取消")
            try:
                return func(**kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                last_error = e
                if attempt < max_retries:
                    delay = self.RETRY_DELAY * attempt
                    self._log(f"[{label}] 网络错误，第{attempt}次重试 (等待{delay}秒): {e}")
                    time.sleep(delay)
                else:
                    self._log(f"[{label}] 网络错误，已重试{max_retries}次，放弃: {e}")
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response else 0
                if status_code in (429, 502, 503, 504):
                    last_error = e
                    if attempt < max_retries:
                        delay = self.RETRY_DELAY * attempt * 2
                        self._log(f"[{label}] 服务器错误({status_code})，第{attempt}次重试 (等待{delay}秒)")
                        time.sleep(delay)
                    else:
                        self._log(f"[{label}] 服务器错误，已重试{max_retries}次，放弃")
                else:
                    raise
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    delay = self.RETRY_DELAY * attempt
                    self._log(f"[{label}] 异常，第{attempt}次重试: {e}")
                    time.sleep(delay)
                else:
                    self._log(f"[{label}] 异常，已重试{max_retries}次，放弃: {e}")
        raise RuntimeError(f"[{label}] 重试{max_retries}次后仍失败: {last_error}")

    def _upload_file(self, file_path: str) -> str:
        fname_only = Path(file_path).name
        size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0
        self._log(f"🔄 正在上传 {fname_only}（{size_mb:.1f}MB，预计 5-60 秒）...")

        def _do_upload():
            url = f"{self._get('api.base_url')}/media/upload/binary"
            headers = {"Authorization": f"Bearer {self._get('api.key')}"}
            with open(file_path, "rb") as f:
                resp = requests.post(url, headers=headers, files={"file": f}, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            fname = data.get("data", {}).get("fileName", "")
            if not fname:
                raise RuntimeError(f"上传返回无效: {data}")
            return fname

        upload_start = time.time()
        try:
            result = self._retry_request(_do_upload, label=f"上传{fname_only}")
            elapsed = time.time() - upload_start
            self._log(f"✅ {fname_only} 上传成功（用了 {elapsed:.1f} 秒）")
            return result
        except Exception as e:
            elapsed = time.time() - upload_start
            self._log(f"❌ {fname_only} 上传失败（已用 {elapsed:.1f} 秒）: {e}")
            raise

    def _submit_audio(self, text: str, sample_fname: str) -> str:
        def _do_submit():
            url = f"{self._get('api.base_url')}/run/ai-app/{self._get('api.audio_workflow')}"
            headers = {
                "Authorization": f"Bearer {self._get('api.key')}",
                "Content-Type": "application/json",
            }
            payload = {
                "nodeInfoList": [
                    {"nodeId": "34", "fieldName": "audio", "fieldValue": sample_fname, "description": "audio"},
                    {"nodeId": "3", "fieldName": "audio", "fieldValue": sample_fname, "description": "audio"},
                    {"nodeId": "9", "fieldName": "text", "fieldValue": f"[S1]{text}", "description": "text"},
                ],
                "instanceType": "default",
                "usePersonalQueue": "false",
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("taskId", "")
            if not task_id:
                raise RuntimeError(f"音频任务提交返回无效: {data}")
            return task_id

        return self._retry_request(_do_submit, label="提交音频任务")

    def _submit_video(self, audio_fname: str, img_fname: str, action_prompt: str) -> str:
        def _do_submit():
            url = f"{self._get('api.base_url')}/run/ai-app/{self._get('api.video_workflow')}"
            headers = {
                "Authorization": f"Bearer {self._get('api.key')}",
                "Content-Type": "application/json",
            }
            payload = {
                "nodeInfoList": [
                    {"nodeId": "444", "fieldName": "image", "fieldValue": img_fname, "description": "image"},
                    {"nodeId": "1755", "fieldName": "audio", "fieldValue": audio_fname, "description": "audio"},
                    {"nodeId": "1624", "fieldName": "value", "fieldValue": action_prompt, "description": "value"},
                ],
                "instanceType": "default",
                "usePersonalQueue": "false",
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("taskId", "")
            if not task_id:
                raise RuntimeError(f"视频任务提交返回无效: {data}")
            return task_id

        return self._retry_request(_do_submit, label="提交视频任务")

    def _submit_video_infinite(self, audio_fname: str, img_fname: str, audio_duration_sec: float) -> str:
        """v1.58：语音+视频 infinite 模式专用，用 nodeId 21/20/6，传 audio_duration 算 end_time"""
        def _do_submit():
            url = f"{self._get('api.base_url')}/run/ai-app/{self._get('api.video_infinite_workflow')}"
            headers = {
                "Authorization": f"Bearer {self._get('api.key')}",
                "Content-Type": "application/json",
            }
            end_time = self._calc_end_time(audio_duration_sec)
            payload = {
                "nodeInfoList": [
                    {"nodeId": "21", "fieldName": "audio", "fieldValue": audio_fname, "description": "audio"},
                    {"nodeId": "20", "fieldName": "image", "fieldValue": img_fname, "description": "image"},
                    {"nodeId": "6", "fieldName": "start_time", "fieldValue": "0:00", "description": "start_time"},
                    {"nodeId": "6", "fieldName": "end_time", "fieldValue": end_time, "description": "end_time"},
                ],
                "instanceType": "default",
                "usePersonalQueue": "false",
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("taskId", "")
            if not task_id:
                raise RuntimeError(f"视频任务(infinite)提交返回无效: {data}")
            return task_id

        return self._retry_request(_do_submit, label="提交视频任务(infinite)")

    def _submit_heygem(self, video_fname: str, prompt: str, ref_audio_fname: str) -> str:
        """v1.67：纯 Heygem 模式（select=2），单工作流跑完数字人：
        - nodeId 1  file    形象视频
        - nodeId 14 select  固定 "2"（参考音+文案）
        - nodeId 12 prompt  文案
        - nodeId 7  audio   人设样本声音（参考音 wav）
        """
        def _do_submit():
            url = f"{self._get('api.base_url')}/run/ai-app/{self._get('api.heygem_workflow')}"
            headers = {
                "Authorization": f"Bearer {self._get('api.key')}",
                "Content-Type": "application/json",
            }
            payload = {
                "nodeInfoList": [
                    {"nodeId": "1", "fieldName": "file", "fieldValue": video_fname, "description": "目标视频"},
                    {"nodeId": "14", "fieldName": "select", "fieldValue": "2", "description": "模式：1语音直接，2人物参考声音+文案"},
                    {"nodeId": "12", "fieldName": "prompt", "fieldValue": prompt, "description": "文案"},
                    {"nodeId": "7", "fieldName": "audio", "fieldValue": ref_audio_fname, "description": "人物参考音"},
                ],
                "instanceType": "default",
                "usePersonalQueue": "false",
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("taskId", "")
            if not task_id:
                raise RuntimeError(f"Heygem 任务提交返回无效: {data}")
            return task_id

        return self._retry_request(_do_submit, label="提交Heygem任务")

    def _poll_task(self, task_id: str, timeout: int, interval: int, label: str, submit_fn=None, prefer_output_type=None, **submit_kwargs) -> dict:
        self._register_task(task_id)
        try:
            for retry in range(self.MAX_TASK_RETRIES + 1):
                start = time.time()
                while self._running:
                    elapsed = int(time.time() - start)
                    if elapsed > timeout:
                        if retry < self.MAX_TASK_RETRIES:
                            self._log(f"[{label}] 超时，第{retry + 1}次重新提交任务...")
                            if submit_fn:
                                try:
                                    self._unregister_task(task_id)
                                    task_id = submit_fn(**submit_kwargs)
                                    self._register_task(task_id)
                                    self._log(f"[{label}] 重新提交成功，新taskId={task_id}")
                                    continue
                                except Exception as e:
                                    self._log(f"[{label}] 重新提交失败: {e}")
                        return {"status": "TIMEOUT"}

                    try:
                        url = f"{self._get('api.base_url')}/query"
                        headers = {
                            "Authorization": f"Bearer {self._get('api.key')}",
                            "Content-Type": "application/json",
                        }
                        resp = requests.post(url, headers=headers, json={"taskId": task_id}, timeout=30)
                        resp.raise_for_status()
                        data = resp.json()
                        status = data.get("status", "")

                        if status == "SUCCESS":
                            results = data.get("results", []) or []
                            # v1.68：多产物工作流（Heygem 会同时返回音频+视频）按 outputType 选
                            url_out = ""
                            matched_type = ""
                            if prefer_output_type:
                                for r in results:
                                    if r.get("outputType", "").lower() == prefer_output_type.lower():
                                        url_out = r.get("url", "")
                                        matched_type = r.get("outputType", "")
                                        break
                                if not url_out:
                                    # 兜底：找不到指定 type 就用第一个
                                    url_out = results[0].get("url", "") if results else ""
                                    matched_type = results[0].get("outputType", "") if results else ""
                                    self._log(f"[{label}] ⚠️ 未找到 outputType={prefer_output_type}，回退到 {matched_type}")
                            else:
                                url_out = results[0].get("url", "") if results else ""
                                matched_type = results[0].get("outputType", "") if results else ""
                            if results and matched_type:
                                self._log(f"[{label}] 完成 ({elapsed}秒, type={matched_type}, 共{len(results)}个产物)")
                            else:
                                self._log(f"[{label}] 完成 ({elapsed}秒)")
                            return {"status": "SUCCESS", "url": url_out}
                        elif status == "FAILED":
                            error = data.get("errorMessage", "未知错误")
                            if retry < self.MAX_TASK_RETRIES:
                                self._log(f"[{label}] 任务失败: {error}，第{retry + 1}次重试...")
                                if submit_fn:
                                    try:
                                        time.sleep(self.RETRY_DELAY)
                                        self._unregister_task(task_id)
                                        task_id = submit_fn(**submit_kwargs)
                                        self._register_task(task_id)
                                        self._log(f"[{label}] 重新提交成功，新taskId={task_id}")
                                        break
                                    except Exception as e:
                                        self._log(f"[{label}] 重新提交失败: {e}")
                                        return {"status": "FAILED", "error": error}
                            else:
                                self._log(f"[{label}] 失败，已重试{self.MAX_TASK_RETRIES}次: {error}")
                                return {"status": "FAILED", "error": error}
                        else:
                            self._log(f"[{label}] 等待中... ({elapsed}秒)")
                    except (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout) as e:
                        self._log(f"[{label}] 查询网络错误，继续轮询: {e}")

                    time.sleep(interval)
                return {"status": "STOPPED"}
            return {"status": "FAILED", "error": "超过最大重试次数"}
        finally:
            self._unregister_task(task_id)

    def _estimate_duration(self, text: str) -> float:
        return len(re.sub(r"\s", "", text)) * self.CHARS_PER_SECOND

    def _split_script(self, text: str) -> list:
        duration = self._estimate_duration(text)
        self._log(f"预估时长: {duration:.1f}秒 ({len(text)}字)")
        if duration <= self.MAX_SEGMENT_DURATION:
            self._log("单段处理，无需拆分")
            return [text]
        segments = []
        sentences = re.split(r"(。|！|？|；)", text)
        current = ""
        for s in sentences:
            if not s.strip():
                continue
            if re.match(r"(。|！|？|；)", s) and current:
                current += s
                if self._estimate_duration(current) >= self.MAX_SEGMENT_DURATION * 0.8:
                    segments.append(current.strip())
                    current = ""
            else:
                test = current + s
                if self._estimate_duration(test) > self.MAX_SEGMENT_DURATION:
                    if current.strip():
                        segments.append(current.strip())
                    current = s
                else:
                    current = test
        if current.strip():
            segments.append(current.strip())
        self._log(f"拆分为 {len(segments)} 段")
        return segments

    def _format_numbers(self, text: str) -> str:
        num_map = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
        unit_map = {0: "", 1: "十", 2: "百", 3: "千", 4: "万", 5: "十", 6: "百", 7: "千", 8: "亿", 9: "十", 10: "百", 11: "千"}

        def int_to_chinese(n_str):
            if not n_str or n_str == "0":
                return "零"
            n_str = n_str.lstrip("0")
            if not n_str:
                return "零"
            n = int(n_str)
            if n < 10:
                return num_map[n_str]
            if n < 20:
                # v1.99 修复：n_str[0]=='1' 时应该是 "十X"（11→十一），而不是 "X十"（11→一十）
                if n_str[0] == '1':
                    return '十' + (num_map[n_str[1]] if n_str[1] != '0' else '')
                return num_map[n_str[0]] + '十' + (num_map[n_str[1]] if n_str[1] != '0' else '')
            # 超过12位的数字（手机号、ID等）逐位朗读
            if len(n_str) > 12:
                return "".join(num_map.get(c, c) for c in n_str)
            result = []
            unit_idx = len(n_str) - 1
            zero_needed = False
            for i, c in enumerate(n_str):
                v = int(c)
                if v == 0:
                    zero_needed = True
                else:
                    if zero_needed and result:
                        result.append("零")
                    if v != 0:
                        result.append(num_map[c])
                        if unit_idx > 0:
                            result.append(unit_map[unit_idx])
                    zero_needed = False
                unit_idx -= 1
            return "".join(result).rstrip("零").replace("零十", "十").replace("零零", "零")

        def dec_to_chinese(s):
            if "." in s:
                int_part, dec_part = s.split(".", 1)
                int_cn = int_to_chinese(int_part) if int_part and int_part != "0" else ""
                dec_cn = "".join(num_map.get(c, c) for c in dec_part)
                if int_cn and dec_cn:
                    return int_cn + "点" + dec_cn
                elif dec_cn:
                    return "零点" + dec_cn
                return int_cn
            return int_to_chinese(s)

        def dec_digits_chinese(s):
            return "".join(num_map.get(c, c) for c in s)

        def pct_chinese(s):
            if "." in s:
                int_part, dec_part = s.split(".", 1)
                int_cn = int_to_chinese(int_part) if int_part and int_part != "0" else ""
                dec_cn = "".join(num_map.get(c, c) for c in dec_part)
                return "百分之" + int_cn + "点" + dec_cn
            return "百分之" + int_to_chinese(s)

        def convert_number_unit(m, unit):
            num_str = m.group(1)
            return dec_to_chinese(num_str) + unit

        # v2.00：C. 删除 emoji（最优先，避免干扰后续规则）
        emoji_pattern = re.compile(
            "["
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F680-\U0001F6FF"  # transport & map
            "\U0001F900-\U0001F9FF"  # supplemental symbols
            "\U0001FA00-\U0001FAFF"
            "\U00002600-\U000027BF"  # misc symbols
            "\U0001F1E0-\U0001F1FF"  # flags
            "]+",
            flags=re.UNICODE,
        )
        text = emoji_pattern.sub("", text)

        # v2.00：D. 长数字先处理（避免被切碎）
        # 11位手机号 → 替换成"手机号"（避免原文已有"电话"导致重复"电话电话号码"）
        text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "手机号", text)
        # 18位身份证 → 替换成"身份证号"
        text = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "身份证号", text)

        # v2.00：I. 0 开头数字（区号、编号）→ 逐位读（避免被 lstrip('0') 丢 0）
        text = re.sub(r"(?<!\d)0\d{2,4}(?!\d)", lambda m: dec_digits_chinese(m.group(0)), text)

        # v1.99：4位年份规则放最前面，去掉 (?!\d) 限制（年份后跟数字如"2026年12月"很常见）
        text = re.sub(r"(\d{4})年", lambda m: dec_digits_chinese(m.group(1)) + "年", text)

        # v1.99：先处理特殊符号（避免数字转换前/被吃掉）
        # ¥数字 → 数字 + 元（如 ¥100 → 一百元）
        text = re.sub(r"¥(\d+\.?\d*)", lambda m: dec_to_chinese(m.group(1)) + "元", text)
        # @xxx → 艾特 xxx（用中文"艾特"，避免后面英文删除规则把"at"也删了）
        text = re.sub(r"@(\S+)", lambda m: "艾特" + m.group(1), text)
        # A&B → A 和 B
        text = re.sub(r"(\S)&(\S)", lambda m: m.group(1) + "和" + m.group(2), text)

        # v1.99：分数处理（在数字转换前，否则 / 会被 re 切开）
        # 数字/数字 → 几分之几（如 1/2 → 二分之一、3/4 → 四分之三）
        text = re.sub(r"(\d+)/(\d+)", lambda m: int_to_chinese(m.group(2)) + "分之" + int_to_chinese(m.group(1)), text)

        # v1.99：数字+/+单位 → 数字+每+单位（如 1000/平方米 → 一千每平方米）
        text = re.sub(r"(\d+\.?\d*)/([一-鿿]+)", lambda m: dec_to_chinese(m.group(1)) + "每" + m.group(2), text)

        # v2.00：A. 数字+英文单位（不用 \b 因为 Python \w 包含中文，改用 (?![A-Za-z\d])）
        # 100㎡ / 100m² → 一百平方米（注意：㎡/m² 是 2 字符，不能用字符类 [㎡m²]）
        text = re.sub(r"(\d+\.?\d*)\s*(?:㎡|m²)(?![A-Za-z\d])", lambda m: dec_to_chinese(m.group(1)) + "平方米", text)
        # 5km → 五公里
        text = re.sub(r"(\d+\.?\d*)km(?![A-Za-z\d])", lambda m: dec_to_chinese(m.group(1)) + "公里", text)
        # 30kg → 三十公斤
        text = re.sub(r"(\d+\.?\d*)kg(?![A-Za-z\d])", lambda m: dec_to_chinese(m.group(1)) + "公斤", text)
        # 180cm → 一百八十厘米
        text = re.sub(r"(\d+\.?\d*)cm(?![A-Za-z\d])", lambda m: dec_to_chinese(m.group(1)) + "厘米", text)
        # 50w / 50W → 五十万（避免匹配已有"万"）
        text = re.sub(r"(\d+\.?\d*)[wW](?!万)", lambda m: dec_to_chinese(m.group(1)) + "万", text)

        # v2.00：B. 删除剩余英文（连续字母，避免 TTS 乱读 LOFT/CBD/iPhone 等）
        text = re.sub(r"[A-Za-z]+", "", text)

        text = re.sub(r"(\d+)万(?![千亿万])", lambda m: convert_number_unit(m, "万"), text)
        text = re.sub(r"(\d+)亿", lambda m: convert_number_unit(m, "亿"), text)
        text = re.sub(r"(\d+\.?\d*)平", lambda m: dec_to_chinese(m.group(1)) + "平", text)
        text = re.sub(r"(\d+\.?\d*)方", lambda m: dec_to_chinese(m.group(1)) + "方", text)
        text = re.sub(r"(\d+\.?\d*)平米", lambda m: dec_to_chinese(m.group(1)) + "平米", text)
        text = re.sub(r"(\d+\.?\d*)平方", lambda m: dec_to_chinese(m.group(1)) + "平方", text)
        text = re.sub(r"(\d+\.?\d*)%", lambda m: pct_chinese(m.group(1)), text)
        text = re.sub(r"(\d+\.?\d*)度", lambda m: dec_to_chinese(m.group(1)) + "度", text)
        text = re.sub(r"(\d+\.?\d*)元", lambda m: dec_to_chinese(m.group(1)) + "元", text)
        text = re.sub(r"(\d+\.?\d*)块", lambda m: dec_to_chinese(m.group(1)) + "块", text)
        text = re.sub(r"(\d+)号(?!\d)", lambda m: convert_number_unit(m, "号"), text)
        text = re.sub(r"(\d+)年(?!\d)", lambda m: convert_number_unit(m, "年"), text)
        text = re.sub(r"(\d+)月", lambda m: convert_number_unit(m, "月"), text)
        text = re.sub(r"(\d+)日", lambda m: convert_number_unit(m, "日"), text)
        text = re.sub(r"(\d+)楼", lambda m: convert_number_unit(m, "楼"), text)
        text = re.sub(r"(\d+)户", lambda m: convert_number_unit(m, "户"), text)
        text = re.sub(r"(\d+\.?\d*)米(?![厘分])", lambda m: dec_to_chinese(m.group(1)) + "米", text)
        text = re.sub(r"(\d+)公里", lambda m: convert_number_unit(m, "公里"), text)
        text = re.sub(r"(\d+)层", lambda m: convert_number_unit(m, "层"), text)
        text = re.sub(r"(\d+)厘", lambda m: convert_number_unit(m, "厘"), text)
        text = re.sub(r"(\d+)秒", lambda m: convert_number_unit(m, "秒"), text)
        text = re.sub(r"(\d+)分钟", lambda m: convert_number_unit(m, "分钟"), text)
        text = re.sub(r"(?<!\d)(\d{4,})(?!\d)", lambda m: int_to_chinese(m.group(1)), text)
        # v2.00：清理删除 emoji/英文后留下的多余空格
        text = re.sub(r"\s{2,}", " ", text)
        return text

    def _calc_end_time(self, duration: float) -> str:
        seconds = math.ceil(duration)
        if seconds < 60:
            return f"0:{seconds:02d}"
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _get_duration(self, path: str) -> float:
        ffprobe_path = self._get_tool_path("ffprobe")
        ffmpeg_path = self._get_tool_path("ffmpeg")
        # 优先用 ffprobe（如果存在）
        if ffprobe_path and os.path.exists(ffprobe_path):
            cmd = [ffprobe_path, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, start_new_session=True)  # v2.10.33: 脱离父进程 session
            if result.returncode == 0:
                return float(result.stdout.strip())
        # 兜底：用 ffmpeg -i 解析时长
        import re
        cmd = [ffmpeg_path, "-i", path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, start_new_session=True)  # v2.10.33: 脱离父进程 session
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
        if not m:
            raise RuntimeError(f"无法探测时长（ffprobe/ffmpeg 都失败）: {path}")
        h, mi, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return h * 3600 + mi * 60 + s

    def _convert_flac_mp3(self, flac_path: str, mp3_path: str):
        ffmpeg_path = self._get_tool_path("ffmpeg")
        cmd = [ffmpeg_path, "-y", "-i", flac_path, "-acodec", "libmp3lame", "-q:a", "2", mp3_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, start_new_session=True)  # v2.10.33: 脱离父进程 session
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg convert failed: {result.stderr}")
        self._log(f"转换: {Path(mp3_path).name}")

    def _trim_video(self, input_path: str, output_path: str, duration: float):
        ffmpeg_path = self._get_tool_path("ffmpeg")
        cmd = [ffmpeg_path, "-y", "-i", input_path, "-t", str(duration), "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, start_new_session=True)  # v2.10.33: 脱离父进程 session
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg trim failed: {result.stderr}")

    def _merge_videos(self, filelist_path: str, output_path: str):
        ffmpeg_path = self._get_tool_path("ffmpeg")
        cmd = [ffmpeg_path, "-f", "concat", "-safe", "0", "-i", filelist_path, "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, start_new_session=True)  # v2.10.33: 脱离父进程 session
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg merge failed: {result.stderr}")
        self._log(f"合并完成: {Path(output_path).name}")

    def _download(self, url: str, save_path: str) -> bool:
        for attempt in range(1, self.MAX_DOWNLOAD_RETRIES + 1):
            try:
                resp = requests.get(url, stream=True, timeout=120)
                resp.raise_for_status()
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
                file_size = os.path.getsize(save_path)
                if file_size < 1000:
                    raise RuntimeError(f"下载文件过小({file_size}字节)，可能不完整")
                return True
            except Exception as e:
                if attempt < self.MAX_DOWNLOAD_RETRIES:
                    delay = self.RETRY_DELAY * attempt
                    self._log(f"下载失败，第{attempt}次重试 (等待{delay}秒): {e}")
                    time.sleep(delay)
                    if os.path.exists(save_path):
                        os.remove(save_path)
                else:
                    self._log(f"下载失败，已重试{self.MAX_DOWNLOAD_RETRIES}次: {e}")
                    return False
        return False

    def _process_audio_segment(self, seg_text, sample_fname, label, work_dir):
        seg_start = time.time()
        try:
            if not self._running:
                return None
            self._log(f"🚀 [音频{label}] 提交到 RunningHub...")
            task_id = self._submit_audio(seg_text, sample_fname)
            self._log(f"⏳ [音频{label}] task_id={task_id[:8]}...，等待 API 返回（最长 {self.AUDIO_TIMEOUT} 秒）")
            result = self._poll_task(
                task_id, self.AUDIO_TIMEOUT, self.AUDIO_POLL_INTERVAL, f"音频{label}",
                submit_fn=self._submit_audio,
                text=seg_text, sample_fname=sample_fname,
            )
            if result["status"] != "SUCCESS":
                self._log(f"❌ [音频{label}] API 失败: {result.get('error', '')}")
                return None

            self._log(f"⬇️ [音频{label}] API 返回成功，下载音频...")
            flac_path = work_dir / f"audio_{label}.flac"
            mp3_path = work_dir / f"audio_{label}.mp3"
            if not self._download(result["url"], str(flac_path)):
                self._log(f"❌ [音频{label}] 下载失败")
                return None
            self._convert_flac_mp3(str(flac_path), str(mp3_path))
            dur = self._get_duration(str(mp3_path))
            elapsed = time.time() - seg_start
            self._log(f"✅ [音频{label}] 完成（用了 {elapsed:.1f} 秒，时长 {dur:.1f}秒）")
            return {"label": label, "mp3": str(mp3_path), "duration": dur}
        except Exception as e:
            elapsed = time.time() - seg_start
            self._log(f"❌ [音频{label}] 异常（已用 {elapsed:.1f} 秒）: {e}")
            return None

    def _process_video_segment(self, audio_result, img_fname, action_prompt, label, work_dir, mode="video"):
        seg_start = time.time()
        try:
            if not self._running:
                return None
            self._log(f"🚀 [视频{label}] 提交到 RunningHub... (mode={mode})")
            audio_fname = self._upload_file(audio_result["mp3"])
            if mode == "video_infinite":
                task_id = self._submit_video_infinite(audio_fname, img_fname, audio_result["duration"])
                result = self._poll_task(
                    task_id, self.VIDEO_TIMEOUT, self.VIDEO_POLL_INTERVAL, f"视频{label}",
                    submit_fn=self._submit_video_infinite,
                    audio_fname=audio_fname, img_fname=img_fname, audio_duration_sec=audio_result["duration"],
                )
            else:
                task_id = self._submit_video(audio_fname, img_fname, action_prompt)
                result = self._poll_task(
                    task_id, self.VIDEO_TIMEOUT, self.VIDEO_POLL_INTERVAL, f"视频{label}",
                    submit_fn=self._submit_video,
                    audio_fname=audio_fname, img_fname=img_fname, action_prompt=action_prompt,
                )
            if result["status"] != "SUCCESS":
                self._log(f"[视频{label}] 失败: {result.get('error', '')}")
                return None

            video_path = work_dir / f"video_{label}.mp4"
            if not self._download(result["url"], str(video_path)):
                self._log(f"[视频{label}] 下载失败")
                return None
            self._log(f"[视频{label}] 完成")
            return {"label": label, "path": str(video_path)}
        except Exception as e:
            self._log(f"[视频{label}] 异常: {e}")
            return None

    def _save_final_video(self, video_results, persona, output_dir, work_dir):
        if not video_results:
            return None
        if len(video_results) > 1:
            self._log("合并视频片段...")
            filelist_path = work_dir / "filelist.txt"
            with open(filelist_path, "w", encoding="utf-8") as f:
                for vr in sorted(video_results, key=lambda x: x["label"]):
                    fpath = vr["path"].replace("\\", "/")
                    f.write(f"file '{fpath}'\n")
            merged_path = work_dir / f"final_{persona}.mp4"
            self._merge_videos(str(filelist_path), str(merged_path))
        else:
            merged_path = Path(video_results[0]["path"])

        persona_dir = Path(output_dir) / persona
        persona_dir.mkdir(parents=True, exist_ok=True)
        date_prefix = datetime.now().strftime("%m%d")
        seq = 1
        for sub_dir in Path(output_dir).iterdir():
            if not sub_dir.is_dir():
                continue
            for f in sub_dir.iterdir():
                if f.is_file() and f.suffix.lower() == ".mp4":
                    name_stem = f.stem
                    if name_stem.startswith(date_prefix) and len(name_stem) == 7 and name_stem[4:].isdigit():
                        existing_seq = int(name_stem[4:])
                        if existing_seq >= seq:
                            seq = existing_seq + 1
        output_name = f"{date_prefix}{seq:03d}.mp4"
        output_path = persona_dir / output_name
        import shutil
        shutil.copy2(str(merged_path), str(output_path))
        return str(output_path)

    def run_heygem(self, persona: str, prompt: str, video_path: str, ref_audio_path: str, output_dir: str, progress_callback=None):
        """v1.67：纯 Heygem 模式（select=2）
        流程：上传形象视频 + 上传参考音 → 提交 4 节点单工作流 → 轮询 → 下载 → 保存
        """
        self._running = True
        start_time = time.time()
        self._log(f"输出目录: {output_dir}")
        self._log(f"形象视频: {os.path.basename(video_path)}")
        self._log(f"参考声音: {os.path.basename(ref_audio_path)}")
        self._log(f"模式：纯 Heygem 数字人（select=2）")
        self._log(f"API Key: {self._get('api.key')[:8]}...")

        if not self._get('api.key'):
            return {"status": "FAILED", "error": "API Key 未设置"}
        if not os.path.exists(video_path):
            return {"status": "FAILED", "error": f"形象视频文件不存在: {video_path}"}
        if not os.path.exists(ref_audio_path):
            return {"status": "FAILED", "error": f"参考声音文件不存在: {ref_audio_path}"}
        if not output_dir:
            return {"status": "FAILED", "error": "输出目录未设置，请在任务页设置输出目录"}

        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return {"status": "FAILED", "error": f"输出目录无权限访问: {output_dir}，请重新设置一个本地目录"}
        except Exception as e:
            return {"status": "FAILED", "error": f"输出目录无效: {output_dir}，错误: {e}"}

        work_dir = Path(output_dir) / "workspace" / persona / datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._log("=" * 50)
            self._log(f"Heygem 数字人生成 - {persona}")
            self._log("=" * 50)

            self._log("上传形象视频...")
            video_fname = self._upload_file(video_path)
            self._log("上传参考声音...")
            ref_audio_fname = self._upload_file(ref_audio_path)

            if progress_callback:
                progress_callback(5, "提交 Heygem 任务...")

            self._log("[Heygem] 提交...")
            task_id = self._submit_heygem(video_fname, prompt, ref_audio_fname)

            if progress_callback:
                progress_callback(15, "Heygem 任务运行中...")

            result = self._poll_task(
                task_id, self.VIDEO_TIMEOUT, self.VIDEO_POLL_INTERVAL, "Heygem",
                submit_fn=self._submit_heygem,
                prefer_output_type="mp4",
                video_fname=video_fname, prompt=prompt, ref_audio_fname=ref_audio_fname,
            )
            if result["status"] != "SUCCESS":
                self._log(f"[Heygem] 失败: {result.get('error', '')}")
                return {"status": "FAILED", "error": result.get("error", "Heygem 任务失败")}

            video_path_out = work_dir / "video_heygem.mp4"
            if not self._download(result["url"], str(video_path_out)):
                return {"status": "FAILED", "error": "Heygem 视频下载失败"}

            # 用 _save_final_video 存到 persona 目录
            output_path = self._save_final_video(
                [{"label": "heygem", "path": str(video_path_out)}],
                persona, output_dir, work_dir,
            )
            if not output_path:
                return {"status": "FAILED", "error": "Heygem 视频保存失败"}

            elapsed = int(time.time() - start_time)
            self._log("=" * 50)
            self._log(f"✅ Heygem 生成完成!")
            self._log(f"人设: {persona}")
            self._log(f"总耗时: {elapsed}秒")
            self._log(f"成品: {output_path}")
            self._log("=" * 50)

            if progress_callback:
                progress_callback(100, f"完成! 耗时 {elapsed}秒")

            self._cleanup_workspace(work_dir)
            return {"status": "SUCCESS", "output": str(output_path), "duration": 0, "elapsed": elapsed}

        except Exception as e:
            self._log(f"❌ Heygem 执行失败: {e}")
            if progress_callback:
                progress_callback(-1, f"失败: {e}")
            return {"status": "FAILED", "error": str(e)}

    def run_heygem_batch(self, tasks: list, output_dir: str, pick_fn=None, progress_callback=None, task_complete_callback=None):
        """v1.72：heygem 模式批量（不调 run_heygem，自己管上传+任务+下载，避免并发 bug）
        流程：
          1. pick_fn 取所有任务的素材（视频 + 参考音）
          2. 全局上传缓存：同人设 (video, audio) 路径只上传一次
          3. 每个任务：提交 Heygem 任务 → 轮询 → 下载 → 保存
          4. work_dir 加 task_idx 区分，避免并发冲突
        """
        self._running = True
        start_time = time.time()

        self._log(f"输出目录: {output_dir}")
        self._log(f"API Key: {self._get('api.key')[:8]}...")
        self._log(f"并发数: {self._get_max_concurrent()}")
        self._log(f"任务数: {len(tasks)}")

        if not self._get('api.key'):
            return [{"status": "FAILED", "error": "API Key 未设置"}] * len(tasks)
        if not output_dir:
            return [{"status": "FAILED", "error": "输出目录未设置"}] * len(tasks)
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return [{"status": "FAILED", "error": f"输出目录无权限: {output_dir}"}] * len(tasks)
        except Exception as e:
            return [{"status": "FAILED", "error": f"输出目录无效: {e}"}] * len(tasks)

        # === Step 1: pick_fn 取所有任务的素材 ===
        task_inputs = []
        for task_idx, task in enumerate(tasks):
            persona = task["persona"]
            script = self._format_numbers(task["script"])

            if pick_fn:
                picked = pick_fn(persona)
                if picked and len(picked) == 3:
                    image_path, sample_audio_path, video_path = picked
                elif picked and len(picked) == 2:
                    video_path, sample_audio_path = picked
                else:
                    video_path, sample_audio_path = None, None
            else:
                video_path, sample_audio_path = None, None

            if not video_path or not sample_audio_path:
                self._log(f"文案{task_idx + 1}: 人设「{persona}」缺形象视频或参考音，跳过")
                task_inputs.append({"status": "skipped", "persona": persona, "task_idx": task_idx, "script": script})
                continue

            task_inputs.append({
                "status": "ready",
                "persona": persona,
                "script": script,
                "video_path": video_path,
                "ref_audio_path": sample_audio_path,
                "task_idx": task_idx,
            })

        ready = [t for t in task_inputs if t["status"] == "ready"]
        if not ready:
            self._log("没有可执行的 Heygem 任务")
            return [{"status": "FAILED", "error": "没有可执行的 Heygem 任务", "task_idx": i} for i in range(len(tasks))]

        self._log(f"\n{'='*50}")
        self._log(f"Heygem 批量 ({len(ready)}条, {self._get_max_concurrent()}并发)")
        self._log(f"{'='*50}")

        if progress_callback:
            progress_callback(5, f"Heygem 准备: {len(ready)}条...")

        # === Step 2: 全局上传缓存（同 path 只上传一次）===
        # 缓存 key = 文件绝对路径，value = server fname
        upload_cache = {}
        upload_lock = threading.Lock()

        def _upload_cached(path):
            """并发安全的去重上传"""
            with upload_lock:
                if path in upload_cache:
                    return upload_cache[path]
            # 不在锁内做实际上传（避免阻塞其他 task）
            fname = self._upload_file(path)
            with upload_lock:
                upload_cache[path] = fname
            return fname

        # 先把每个人设的视频/参考音上传（去重后实际可能上传 < 2n 次）
        # 收集所有 (task_idx, kind, path)
        upload_jobs = []
        for ti in ready:
            upload_jobs.append((ti["task_idx"], "video", ti["video_path"]))
            upload_jobs.append((ti["task_idx"], "ref_audio", ti["ref_audio_path"]))

        uploaded = {}  # task_idx -> {"video_fname": str, "ref_audio_fname": str}

        with ThreadPoolExecutor(max_workers=self._get_max_concurrent()) as pool:
            upload_futures = {}
            for task_idx, kind, path in upload_jobs:
                key = (kind, path)
                with upload_lock:
                    already = key in upload_cache
                if already:
                    continue
                fut = pool.submit(_upload_cached, path)
                upload_futures[fut] = (task_idx, kind, path)

            done = 0
            for fut in as_completed(upload_futures):
                if not self._running:
                    pool.shutdown(wait=False)
                    break
                task_idx, kind, path = upload_futures[fut]
                try:
                    fname = fut.result()
                except Exception as e:
                    self._log(f"[T{task_idx + 1}] {kind} 上传失败: {e}")
                    # 标记这个 task 为 failed
                    if task_idx not in uploaded:
                        uploaded[task_idx] = {"error": str(e)}
                    done += 1
                    continue

                # 把 fname 写入到所属 task
                if task_idx not in uploaded:
                    uploaded[task_idx] = {}
                if "error" in uploaded[task_idx]:
                    continue
                uploaded[task_idx][f"{kind}_fname"] = fname
                done += 1
                if progress_callback:
                    pct = 5 + int(30 * done / len(upload_futures))
                    progress_callback(pct, f"上传 {done}/{len(upload_futures)}")

            # 处理已缓存的（即没触发 future 的）
            for task_idx, kind, path in upload_jobs:
                with upload_lock:
                    cached = upload_cache.get(path)
                if cached:
                    if task_idx not in uploaded:
                        uploaded[task_idx] = {}
                    if "error" not in uploaded[task_idx]:
                        uploaded[task_idx][f"{kind}_fname"] = cached

        # 检查哪些 task 上传成功
        valid_tasks = []
        for ti in ready:
            ti_idx = ti["task_idx"]
            info = uploaded.get(ti_idx, {})
            if "error" in info or "video_fname" not in info or "ref_audio_fname" not in info:
                continue
            ti["video_fname"] = info["video_fname"]
            ti["ref_audio_fname"] = info["ref_audio_fname"]
            valid_tasks.append(ti)

        if not valid_tasks:
            self._log("所有 Heygem 任务上传阶段失败")
            return [{"status": "FAILED", "error": "上传阶段失败", "task_idx": i} for i in range(len(tasks))]

        # === Step 3: 提交任务 + 轮询 + 下载 ===
        self._log(f"\n{'='*50}")
        self._log(f"Heygem 任务并发 ({len(valid_tasks)}条)")
        self._log(f"{'='*50}")

        if progress_callback:
            progress_callback(40, f"Heygem 提交: {len(valid_tasks)}条...")

        results = [None] * len(tasks)

        def _one_heygem(ti):
            """单条 Heygem 任务：提交 → 轮询 → 下载 → 保存"""
            t_start = time.time()
            self._log(f"[T{ti['task_idx'] + 1}] {ti['persona']}: 提交 Heygem 任务...")
            try:
                task_id = self._submit_heygem(
                    ti["video_fname"], ti["script"], ti["ref_audio_fname"]
                )
            except Exception as e:
                return {"status": "FAILED", "error": f"提交失败: {e}", "task_idx": ti["task_idx"], "elapsed": 0}

            self._log(f"[T{ti['task_idx'] + 1}] {ti['persona']}: 等待结果 (task_id={task_id[:8]}...)")
            result = self._poll_task(
                task_id, self.VIDEO_TIMEOUT, self.VIDEO_POLL_INTERVAL,
                f"hey_{ti['task_idx'] + 1}",
                submit_fn=self._submit_heygem,
                prefer_output_type="mp4",
                video_fname=ti["video_fname"],
                prompt=ti["script"],
                ref_audio_fname=ti["ref_audio_fname"],
            )
            if result["status"] != "SUCCESS":
                return {"status": "FAILED", "error": result.get("error", "轮询失败"), "task_idx": ti["task_idx"], "elapsed": int(time.time() - t_start)}

            # 下载到独立 work_dir（加 task_idx 避免冲突）
            ts = datetime.now().strftime("%H%M%S_%f")[:-3]  # 毫秒精度
            work_dir = Path(output_dir) / "workspace" / ti["persona"] / f"hey_T{ti['task_idx'] + 1}_{ts}"
            try:
                work_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {"status": "FAILED", "error": f"创建 work_dir 失败: {e}", "task_idx": ti["task_idx"], "elapsed": int(time.time() - t_start)}

            video_out = work_dir / "video_heygem.mp4"
            if not self._download(result["url"], str(video_out)):
                return {"status": "FAILED", "error": "下载失败", "task_idx": ti["task_idx"], "elapsed": int(time.time() - t_start)}

            # 保存到 persona 目录（用 _save_final_video，路径分隔有 task_idx 不会冲突）
            output_path = self._save_final_video(
                [{"label": f"hey_T{ti['task_idx'] + 1}", "path": str(video_out)}],
                ti["persona"], output_dir, work_dir,
            )
            if not output_path:
                return {"status": "FAILED", "error": "保存失败", "task_idx": ti["task_idx"], "elapsed": int(time.time() - t_start)}

            elapsed = int(time.time() - t_start)
            return {
                "status": "SUCCESS",
                "output": str(output_path),
                "duration": 0,
                "persona": ti["persona"],
                "task_idx": ti["task_idx"],
                "elapsed": elapsed,
            }

        with ThreadPoolExecutor(max_workers=self._get_max_concurrent()) as pool:
            fut_to_ti = {pool.submit(_one_heygem, ti): ti for ti in valid_tasks}
            done = 0
            for fut in as_completed(fut_to_ti):
                if not self._running:
                    pool.shutdown(wait=False)
                    for ti in valid_tasks:
                        if results[ti["task_idx"]] is None:
                            results[ti["task_idx"]] = {"status": "FAILED", "error": "用户取消", "task_idx": ti["task_idx"], "persona": ti["persona"]}
                    break
                ti = fut_to_ti[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"status": "FAILED", "error": str(e), "task_idx": ti["task_idx"], "persona": ti["persona"]}
                results[ti["task_idx"]] = r
                done += 1
                icon = "✓" if r["status"] == "SUCCESS" else "✗"
                self._log(f"[T{ti['task_idx'] + 1}] {ti['persona']} {icon} ({done}/{len(valid_tasks)}, {r.get('elapsed', 0)}秒)")

                if progress_callback:
                    pct = 40 + int(55 * done / len(valid_tasks))
                    progress_callback(pct, f"Heygem {done}/{len(valid_tasks)}")

                if task_complete_callback and r["status"] == "SUCCESS":
                    try:
                        task_complete_callback(r)
                    except Exception:
                        pass

        # 填没跑到的占位
        for i, r in enumerate(results):
            if r is None:
                ti = task_inputs[i]
                if ti["status"] == "skipped":
                    results[i] = {"status": "FAILED", "error": f"人设「{ti['persona']}」缺素材", "task_idx": i, "persona": ti["persona"]}
                else:
                    results[i] = {"status": "FAILED", "error": "未跑", "task_idx": i, "persona": ti["persona"]}

        # 清理所有 work_dir（按 persona 整理）
        try:
            ws_root = Path(output_dir) / "workspace"
            if ws_root.exists():
                import shutil
                shutil.rmtree(str(ws_root), ignore_errors=True)
                self._log("临时文件已清理")
        except Exception:
            pass

        elapsed = int(time.time() - start_time)
        success_count = sum(1 for r in results if r["status"] == "SUCCESS")
        fail_count = sum(1 for r in results if r["status"] != "SUCCESS")

        self._log(f"\n{'='*50}")
        self._log(f"Heygem 批量完成! 成功: {success_count}, 失败: {fail_count}, 总耗时: {elapsed}秒")
        self._log(f"{'='*50}")

        if progress_callback:
            progress_callback(100, f"完成! 成功{success_count} 失败{fail_count}")

        return results

    def _cleanup_workspace(self, work_dir):
        try:
            import shutil
            wd = Path(work_dir) if not isinstance(work_dir, Path) else work_dir
            if wd.exists():
                shutil.rmtree(str(wd), ignore_errors=True)
                self._log(f"临时文件已清理")
        except Exception:
            pass

    def run(self, persona: str, script: str, image_path: str, sample_audio_path: str, output_dir: str, action_prompt: str = "", progress_callback=None, mode: str = "video"):
        self._running = True
        start_time = time.time()
        self._log(f"输出目录: {output_dir}")
        self._log(f"图片路径: {image_path}")
        self._log(f"声音路径: {sample_audio_path}")
        if action_prompt:
            self._log(f"动作提示: {action_prompt[:30]}...")
        self._log(f"视频模式: {mode}")
        self._log(f"API Key: {self._get('api.key')[:8]}...")

        if not self._get('api.key'):
            return {"status": "FAILED", "error": "API Key 未设置"}
        if not os.path.exists(image_path):
            return {"status": "FAILED", "error": f"图片文件不存在: {image_path}"}
        if not os.path.exists(sample_audio_path):
            return {"status": "FAILED", "error": f"声音文件不存在: {sample_audio_path}"}
        if not output_dir:
            return {"status": "FAILED", "error": "输出目录未设置，请在任务页设置输出目录"}
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return {"status": "FAILED", "error": f"输出目录无权限访问: {output_dir}，请重新设置一个本地目录"}
        except Exception as e:
            return {"status": "FAILED", "error": f"输出目录无效: {output_dir}，错误: {e}"}

        work_dir = Path(output_dir) / "workspace" / persona / datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._log("=" * 50)
            self._log(f"口播视频生成 - {persona}")
            self._log("=" * 50)

            script = self._format_numbers(script)
            self._log("数字格式化完成")

            self._log("上传样本音...")
            sample_fname = self._upload_file(sample_audio_path)
            self._log("上传形象图片...")
            img_fname = self._upload_file(image_path)

            segments = self._split_script(script)
            total_segments = len(segments)

            if progress_callback:
                progress_callback(0, f"音频阶段，共{total_segments}条...")

            audio_futures = {}
            with ThreadPoolExecutor(max_workers=self._get_max_concurrent()) as audio_pool:
                for i, seg in enumerate(segments):
                    if not self._running:
                        raise RuntimeError("用户取消")
                    label = f"S{i + 1}"
                    future = audio_pool.submit(
                        self._process_audio_segment, seg, sample_fname, label, work_dir
                    )
                    audio_futures[future] = label

                audio_results = {}
                for future in as_completed(audio_futures):
                    if not self._running:
                        raise RuntimeError("用户取消")
                    label = audio_futures[future]
                    result = future.result()
                    if result:
                        audio_results[label] = result
                        self._log(f"音频{label} ✓")
                        if progress_callback:
                            pct = int(len(audio_results) / total_segments * 30)
                            progress_callback(pct, f"音频{pct}%")
                    else:
                        self._log(f"音频{label} ✗")

            if not audio_results:
                raise RuntimeError("所有音频生成失败")

            total_audio_duration = sum(ar.get("duration", 0) for ar in audio_results.values())
            estimated_video_time = 0
            if progress_callback and total_audio_duration > 0:
                max_concurrent = self._get_max_concurrent()
                audio_durations = [ar.get("duration", 0) for ar in audio_results.values()]
                audio_durations.sort(reverse=True)
                total_batches = (len(audio_durations) + max_concurrent - 1) // max_concurrent
                pessimistic_time = 0
                for i in range(total_batches):
                    batch_start = i * max_concurrent
                    batch_end = min(batch_start + max_concurrent, len(audio_durations))
                    batch_max = max(audio_durations[batch_start:batch_end]) if audio_durations[batch_start:batch_end] else 0
                    pessimistic_time += batch_max * 24.5
                if total_batches == 1:
                    correction_factor = 1.0
                elif total_batches == 2:
                    correction_factor = 0.95
                elif total_batches == 3:
                    correction_factor = 0.90
                elif total_batches == 4:
                    correction_factor = 0.88
                else:
                    correction_factor = 0.87
                estimated_video_time = int(pessimistic_time * correction_factor)
                progress_callback(31, f"预估{estimated_video_time/60:.0f}分")

            video_futures = {}
            video_total_count = len(audio_results)
            video_done_count = [0]
            import time as time_module
            estimated_video_start_time = time_module.time()

            with ThreadPoolExecutor(max_workers=self._get_max_concurrent()) as video_pool:
                for label, ar in audio_results.items():
                    if not self._running:
                        raise RuntimeError("用户取消")
                    future = video_pool.submit(
                        self._process_video_segment, ar, img_fname, action_prompt, label, work_dir, mode
                    )
                    video_futures[future] = label

                video_results = []
                for future in as_completed(video_futures):
                    if not self._running:
                        raise RuntimeError("用户取消")
                    label = video_futures[future]
                    result = future.result()
                    if result:
                        video_results.append(result)
                        self._log(f"视频{label} ✓")
                    else:
                        self._log(f"视频{label} ✗")
                    video_done_count[0] += 1
                    if estimated_video_time > 0 and progress_callback:
                        elapsed = time_module.time() - estimated_video_start_time
                        remaining = max(0, estimated_video_time - elapsed)
                        pct = 31 + int((98 - 31) * (1 - remaining / max(estimated_video_time, 1)))
                        pct = min(98, max(31, pct))
                        progress_callback(pct, f"预估{int(remaining/60)}分{int(remaining%60)}秒")

            if not video_results:
                raise RuntimeError("所有视频生成失败")

            if progress_callback:
                progress_callback(85, "合并输出...")

            output_path = self._save_final_video(video_results, persona, output_dir, work_dir)
            if not output_path:
                raise RuntimeError("视频合并/保存失败")

            total_dur = sum(ar["duration"] for ar in audio_results.values())
            elapsed = int(time.time() - start_time)

            self._log("=" * 50)
            self._log(f"✅ 生成完成!")
            self._log(f"人设: {persona}")
            self._log(f"视频时长: {total_dur:.1f}秒")
            self._log(f"总耗时: {elapsed}秒")
            self._log(f"成品: {output_path}")
            self._log("=" * 50)

            if progress_callback:
                progress_callback(100, f"完成! 视频时长 {total_dur:.1f}秒")

            self._cleanup_workspace(work_dir)

            return {"status": "SUCCESS", "output": str(output_path), "duration": total_dur, "elapsed": elapsed}

        except Exception as e:
            self._log(f"❌ 执行失败: {e}")
            if progress_callback:
                progress_callback(-1, f"失败: {e}")
            return {"status": "FAILED", "error": str(e)}

    def run_audio_only(self, persona: str, script: str, sample_audio_path: str, output_dir: str, progress_callback=None):
        """仅生成语音，不生成视频"""
        self._running = True
        start_time = time.time()
        self._log(f"输出目录: {output_dir}")
        self._log(f"声音路径: {sample_audio_path}")
        self._log(f"模式：仅生成音频")
        self._log(f"API Key: {self._get('api.key')[:8]}...")

        if not self._get('api.key'):
            return {"status": "FAILED", "error": "API Key 未设置"}
        if not os.path.exists(sample_audio_path):
            return {"status": "FAILED", "error": f"声音文件不存在: {sample_audio_path}"}
        if not output_dir:
            return {"status": "FAILED", "error": "输出目录未设置，请在任务页设置输出目录"}

        work_dir = Path(output_dir) / "workspace" / persona / datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._log("=" * 50)
            self._log(f"语音生成 - {persona}")
            self._log("=" * 50)

            script = self._format_numbers(script)
            self._log("数字格式化完成")

            self._log("上传样本音...")
            sample_fname = self._upload_file(sample_audio_path)

            segments = self._split_script(script)
            total_segments = len(segments)

            if progress_callback:
                progress_callback(0, f"音频阶段，共{total_segments}条...")

            audio_futures = {}
            with ThreadPoolExecutor(max_workers=self._get_max_concurrent()) as audio_pool:
                for i, seg in enumerate(segments):
                    if not self._running:
                        raise RuntimeError("用户取消")
                    label = f"S{i + 1}"
                    future = audio_pool.submit(
                        self._process_audio_segment, seg, sample_fname, label, work_dir
                    )
                    audio_futures[future] = label

                audio_results = {}
                for future in as_completed(audio_futures):
                    if not self._running:
                        raise RuntimeError("用户取消")
                    label = audio_futures[future]
                    result = future.result()
                    if result:
                        audio_results[label] = result
                        self._log(f"音频{label} ✓")
                        if progress_callback:
                            pct = int(len(audio_results) / total_segments * 80)
                            progress_callback(pct, f"音频{pct}%")
                    else:
                        self._log(f"音频{label} ✗")

            if not audio_results:
                raise RuntimeError("所有音频生成失败")

            # 合并音频并保存
            self._log("合并音频片段...")
            from pydub import AudioSegment
            from pydub.utils import which
            ffmpeg_path = self._get_tool_path("ffmpeg")
            # 配置 pydub 使用内置 ffmpeg（包含 probe）
            AudioSegment.ffmpeg = ffmpeg_path
            AudioSegment.converter = ffmpeg_path
            AudioSegment.ffprobe = ffmpeg_path  # 用 ffmpeg 代替 ffprobe

            combined = AudioSegment.empty()
            for label in sorted(audio_results.keys()):
                combined += AudioSegment.from_mp3(audio_results[label]["mp3"])

            persona_dir = Path(output_dir) / persona
            persona_dir.mkdir(parents=True, exist_ok=True)
            date_prefix = datetime.now().strftime("%m%d")
            seq = 1
            for sub_dir in Path(output_dir).iterdir():
                if not sub_dir.is_dir():
                    continue
                for f in sub_dir.iterdir():
                    if f.is_file() and f.suffix.lower() == ".mp3":
                        name_stem = f.stem
                        if name_stem.startswith(date_prefix) and len(name_stem) == 7 and name_stem[4:].isdigit():
                            existing_seq = int(name_stem[4:])
                            if existing_seq >= seq:
                                seq = existing_seq + 1

            output_name = f"{date_prefix}{seq:03d}.mp3"
            output_path = persona_dir / output_name
            combined.export(str(output_path), format="mp3")

            total_dur = len(combined) / 1000.0
            elapsed = int(time.time() - start_time)

            self._log("=" * 50)
            self._log(f"✅ 生成完成!")
            self._log(f"人设: {persona}")
            self._log(f"音频时长: {total_dur:.1f}秒")
            self._log(f"总耗时: {elapsed}秒")
            self._log(f"成品: {output_path}")
            self._log("=" * 50)

            if progress_callback:
                progress_callback(100, f"完成! 音频时长 {total_dur:.1f}秒")

            self._cleanup_workspace(work_dir)

            return {"status": "SUCCESS", "output": str(output_path), "duration": total_dur, "elapsed": elapsed}

        except Exception as e:
            self._log(f"❌ 执行失败: {e}")
            if progress_callback:
                progress_callback(-1, f"失败: {e}")
            return {"status": "FAILED", "error": str(e)}

    def _execute_batch_round(self, tasks, output_dir, pick_fn, progress_callback=None, progress_offset=0, progress_scale=1.0, task_complete_callback=None, mode="video"):
        all_audio_tasks = []
        all_video_tasks = []
        task_meta = {}

        for task_idx, task in enumerate(tasks):
            persona = task["persona"]
            script = self._format_numbers(task["script"])
            image_path, sample_audio_path = pick_fn(persona) if pick_fn else (None, None)

            if not image_path or not sample_audio_path:
                self._log(f"文案{task_idx + 1}: 人设「{persona}」文件缺失，跳过")
                task_meta[task_idx] = {"status": "skipped", "persona": persona}
                continue

            work_dir = Path(output_dir) / "workspace" / persona / datetime.now().strftime("%Y%m%d_%H%M%S")
            work_dir.mkdir(parents=True, exist_ok=True)

            self._log(f"文案{task_idx + 1} [{persona}]: 上传样本音...")
            try:
                sample_fname = self._upload_file(sample_audio_path)
            except Exception as e:
                self._log(f"文案{task_idx + 1}: 上传样本音失败 - {e}")
                task_meta[task_idx] = {"status": "skipped", "persona": persona}
                continue
            self._log(f"文案{task_idx + 1} [{persona}]: 上传形象图...")
            try:
                img_fname = self._upload_file(image_path)
            except Exception as e:
                self._log(f"文案{task_idx + 1}: 上传形象图失败 - {e}")
                task_meta[task_idx] = {"status": "skipped", "persona": persona}
                continue

            segments = self._split_script(script)
            self._log(f"文案{task_idx + 1}: 拆分为{len(segments)}段")

            audio_items = []
            for seg_i, seg in enumerate(segments):
                label = f"T{task_idx + 1}_S{seg_i + 1}"
                audio_items.append({
                    "label": label,
                    "text": seg,
                    "sample_fname": sample_fname,
                    "work_dir": work_dir,
                    "task_idx": task_idx,
                })

            all_audio_tasks.extend(audio_items)
            task_meta[task_idx] = {
                "status": "pending",
                "persona": persona,
                "image_fname": img_fname,
                "work_dir": work_dir,
                "audio_labels": [a["label"] for a in audio_items],
                "video_results": [],
            }

        if not all_audio_tasks:
            self._log("没有可执行的任务")
            return [{"status": "FAILED", "error": "没有可执行的任务", "task_idx": i} for i in range(len(tasks))], {}

        self._log(f"\n{'='*50}")
        self._log(f"阶段1: 音频并发处理 ({len(all_audio_tasks)}段, {self._get_max_concurrent()}并发)")
        self._log(f"{'='*50}")

        if progress_callback:
            pct = progress_offset + int(5 * progress_scale)
            progress_callback(pct, f"音频阶段: {len(all_audio_tasks)}段并发处理...")

        audio_results = {}
        import time as _time_audio
        audio_phase_start = _time_audio.time()
        last_heartbeat = audio_phase_start
        with ThreadPoolExecutor(max_workers=self._get_max_concurrent()) as pool:
            future_map = {}
            for at in all_audio_tasks:
                future = pool.submit(
                    self._process_audio_segment,
                    at["text"], at["sample_fname"], at["label"], at["work_dir"]
                )
                future_map[future] = at

            done_count = 0
            for future in as_completed(future_map):
                if not self._running:
                    self._log("用户取消，停止处理")
                    pool.shutdown(wait=False)
                    return [{"status": "FAILED", "error": "用户取消", "task_idx": i} for i in range(len(tasks))], {}

                at = future_map[future]
                result = future.result()
                done_count += 1

                # v2.10.12 心跳：每 30s 打印当前进度，让用户知道软件在动
                now = _time_audio.time()
                if now - last_heartbeat >= 30:
                    elapsed_min = (now - audio_phase_start) / 60
                    self._log(f"⏳ [心跳] 音频阶段已用 {elapsed_min:.1f} 分钟，进度 {done_count}/{len(all_audio_tasks)}，剩余 {len(all_audio_tasks) - done_count} 段")
                    last_heartbeat = now

                if result:
                    audio_results[at["label"]] = result
                    self._log(f"[音频{at['label']}] ✓ ({done_count}/{len(all_audio_tasks)})")

                    video_task = {
                        "label": at["label"],
                        "audio_result": result,
                        "img_fname": task_meta[at["task_idx"]]["image_fname"],
                        "work_dir": at["work_dir"],
                        "task_idx": at["task_idx"],
                        "action_prompt": task.get("action_prompt", ""),
                    }
                    all_video_tasks.append(video_task)
                else:
                    self._log(f"[音频{at['label']}] ✗ ({done_count}/{len(all_audio_tasks)})")

                if progress_callback:
                    pct = progress_offset + int((5 + 35 * done_count / len(all_audio_tasks)) * progress_scale)
                    progress_callback(pct, f"音频 {done_count}/{len(all_audio_tasks)}")

        audio_phase_elapsed = _time_audio.time() - audio_phase_start
        self._log(f"\n音频完成: {len(audio_results)}/{len(all_audio_tasks)}（用了 {audio_phase_elapsed/60:.1f} 分钟）")

        if not all_video_tasks:
            self._log("所有音频生成失败，无法继续")
            return [{"status": "FAILED", "error": "音频生成失败", "task_idx": i} for i in range(len(tasks))], {}

        all_video_tasks.sort(key=lambda x: x["audio_result"].get("duration", 0), reverse=True)

        all_audio_durations = [ar.get("duration", 0) for ar in audio_results.values()]
        estimated_video_time = 0
        if all_audio_durations and progress_callback:
            max_concurrent = self._get_max_concurrent()
            all_audio_durations.sort(reverse=True)
            total_batches = (len(all_audio_durations) + max_concurrent - 1) // max_concurrent
            pessimistic_time = 0
            for i in range(total_batches):
                batch_start = i * max_concurrent
                batch_end = min(batch_start + max_concurrent, len(all_audio_durations))
                batch_max = max(all_audio_durations[batch_start:batch_end]) if all_audio_durations[batch_start:batch_end] else 0
                pessimistic_time += batch_max * 24.5
            correction_factor = 1.0
            estimated_video_time = int(pessimistic_time * correction_factor)
            progress_callback(40, f"预估{estimated_video_time/60:.0f}分")

        self._log(f"\n{'='*50}")
        self._log(f"阶段2: 视频并发处理 ({len(all_video_tasks)}段, {self._get_max_concurrent()}并发)")
        self._log(f"{'='*50}")

        video_results_map = {}
        import time as time_module
        video_start_time = time_module.time()
        with ThreadPoolExecutor(max_workers=self._get_max_concurrent()) as pool:
            future_map = {}
            for vt in all_video_tasks:
                future = pool.submit(
                    self._process_video_segment,
                    vt["audio_result"], vt["img_fname"], vt.get("action_prompt", ""), vt["label"], vt["work_dir"], mode
                )
                future_map[future] = vt

            done_count = 0
            for future in as_completed(future_map):
                if not self._running:
                    self._log("用户取消，停止处理")
                    pool.shutdown(wait=False)
                    return [{"status": "FAILED", "error": "用户取消", "task_idx": i} for i in range(len(tasks))], {}

                vt = future_map[future]
                result = future.result()
                done_count += 1

                if result:
                    video_results_map[vt["label"]] = result
                    task_idx = vt["task_idx"]
                    task_meta[task_idx]["video_results"].append(result)
                    self._log(f"[视频{vt['label']}] ✓ ({done_count}/{len(all_video_tasks)})")
                else:
                    self._log(f"[视频{vt['label']}] ✗ ({done_count}/{len(all_video_tasks)})")

                if progress_callback:
                    if estimated_video_time > 0:
                        elapsed = time_module.time() - video_start_time
                        remaining = max(0, estimated_video_time - elapsed)
                        pct = 40 + int(48 * (1 - remaining / max(estimated_video_time, 1)))
                        pct = min(88, max(40, pct))
                        progress_callback(pct, f"预估{int(remaining/60)}分{int(remaining%60)}秒")
                    else:
                        pct = 40 + int(48 * done_count / len(all_video_tasks))
                        progress_callback(pct, f"视频 {done_count}/{len(all_video_tasks)}")

        self._log(f"\n视频完成: {len(video_results_map)}/{len(all_video_tasks)}")

        self._log(f"\n{'='*50}")
        self._log(f"阶段3: 合并输出")
        self._log(f"{'='*50}")

        if progress_callback:
            pct = progress_offset + int(88 * progress_scale)
            progress_callback(pct, "合并输出中...")

        results = []
        for task_idx, task in enumerate(tasks):
            meta = task_meta.get(task_idx, {})
            if meta.get("status") == "skipped":
                results.append({"status": "FAILED", "error": f"人设「{task['persona']}」文件缺失", "task_idx": task_idx})
                continue

            video_results = meta.get("video_results", [])
            persona = meta.get("persona", task["persona"])
            work_dir = meta.get("work_dir")

            if not video_results:
                results.append({"status": "FAILED", "error": "视频生成失败", "task_idx": task_idx})
                continue

            output_path = self._save_final_video(video_results, persona, output_dir, work_dir)
            if output_path:
                total_dur = sum(
                    audio_results.get(vr["label"], {}).get("duration", 0)
                    for vr in video_results
                )
                self._log(f"文案{task_idx + 1} [{persona}]: ✅ {output_path}")
                result = {
                    "status": "SUCCESS",
                    "output": output_path,
                    "duration": total_dur,
                    "persona": persona,
                    "task_idx": task_idx,
                }
                results.append(result)
                if task_complete_callback:
                    try:
                        task_complete_callback(result)
                    except Exception:
                        pass
            else:
                results.append({"status": "FAILED", "error": "合并输出失败", "task_idx": task_idx})

        return results, task_meta

    def run_batch(self, tasks: list, output_dir: str, pick_fn=None, progress_callback=None, task_complete_callback=None, mode="video"):
        self._running = True
        start_time = time.time()

        self._log(f"输出目录: {output_dir}")
        self._log(f"API Key: {self._get('api.key')[:8]}...")
        self._log(f"并发数: {self._get_max_concurrent()}")
        self._log(f"任务数: {len(tasks)}")
        # v2.10.12 预计总时长提示：让用户知道 batch 不是卡死，是慢
        est_min_low = max(1, len(tasks) // self._get_max_concurrent() // 2)
        est_min_high = max(2, len(tasks) // self._get_max_concurrent() * 2)
        self._log(f"⏱️ 预计总时长: {est_min_low}-{est_min_high} 分钟（{len(tasks)} task / {self._get_max_concurrent()} 并发）")

        if not self._get('api.key'):
            return [{"status": "FAILED", "error": "API Key 未设置"}] * len(tasks)
        if not output_dir:
            return [{"status": "FAILED", "error": "输出目录未设置"}] * len(tasks)
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return [{"status": "FAILED", "error": f"输出目录无权限: {output_dir}"}] * len(tasks)
        except Exception as e:
            return [{"status": "FAILED", "error": f"输出目录无效: {e}"}] * len(tasks)

        results, task_meta = self._execute_batch_round(tasks, output_dir, pick_fn, progress_callback, progress_offset=0, progress_scale=0.9, task_complete_callback=task_complete_callback, mode=mode)

        failed_indices = [i for i, r in enumerate(results) if r["status"] != "SUCCESS"]
        if failed_indices:
            self._log(f"\n{'='*50}")
            self._log(f"重试: {len(failed_indices)}个失败任务")
            self._log(f"{'='*50}")

            retry_tasks = []
            for i in failed_indices:
                retry_tasks.append(tasks[i])

            retry_results, _ = self._execute_batch_round(retry_tasks, output_dir, pick_fn, progress_callback, progress_offset=90, progress_scale=0.1, task_complete_callback=task_complete_callback, mode=mode)

            for i, rr in zip(failed_indices, retry_results):
                if rr["status"] == "SUCCESS":
                    results[i] = rr
                    self._log(f"重试文案{i + 1}: ✅ 成功")
                else:
                    self._log(f"重试文案{i + 1}: ❌ 仍然失败 - {rr.get('error', '')}")

        elapsed = int(time.time() - start_time)
        success_count = sum(1 for r in results if r["status"] == "SUCCESS")
        fail_count = sum(1 for r in results if r["status"] != "SUCCESS")

        cleaned_dirs = set()
        for task_idx, meta in task_meta.items():
            wd = meta.get("work_dir")
            if wd and str(wd) not in cleaned_dirs:
                self._cleanup_workspace(wd)
                cleaned_dirs.add(str(wd))

        self._log(f"\n{'='*50}")
        self._log(f"全部完成! 成功: {success_count}, 失败: {fail_count}, 总耗时: {elapsed}秒")
        self._log(f"{'='*50}")

        if progress_callback:
            progress_callback(100, f"完成! 成功{success_count} 失败{fail_count}")

        return results
