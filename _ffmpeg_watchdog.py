"""
_ffmpeg_watchdog.py - ffmpeg SIGSTOP 看门狗（v2.10.15 累计 T 状态总时长）

父进程（主 app）启动本脚本作为独立进程：
    subprocess.Popen([sys.executable, "_ffmpeg_watchdog.py", str(ffmpeg_pid), label])

本脚本：
  1. os.setpgrp() 脱离父进程组 → 父进程被 SIGSTOP 不影响本脚本
  2. 每 5s 检查 ffmpeg PID 状态
  3. 发现 T 状态（SIGSTOP）→ 升级策略：
       a. 累计 T 状态总时长 < 5s：SIGCONT 单进程 + PGID 试救
       b. 累计 T 状态总时长 ≥ 5s：macOS SIGCONT 对 T 状态无效，
          直接 SIGKILL ffmpeg（让 ASR 拿到非 0 退出码走 except）
       c. 兜底：ffmpeg 总运行时长 > 120s → 强制 SIGKILL
  4. 写到 /tmp/kele_watchdog.log（自己的日志，跟主 app 分开）
  5. ffmpeg 进程结束 → 自己退出

v2.10.15 patch（关键修复）：
  之前 v2.10.10/11 的 t_state_enter_time 在 ffmpeg 短暂退出 T 又进 T 时会被重置，
  导致 30s SIGKILL 升级永远触发不了。改：累计 T 状态总时长（不重置），
  累计 ≥ 5s 立刻 SIGKILL，不再依赖 SIGCONT 能否救回。
  macOS 现实：SIGCONT 对 ffmpeg 在 T 状态基本无效（实测），所以早期强升级。

使用：
  python _ffmpeg_watchdog.py <ffmpeg_pid> <label>
"""
import sys
import os
import time
import signal
import subprocess

# 脱离父进程进程组（独立 session）
try:
    os.setpgrp()
except Exception:
    pass

if len(sys.argv) < 2:
    sys.exit(1)

ffmpeg_pid = int(sys.argv[1])
label = sys.argv[2] if len(sys.argv) > 2 else "ffmpeg"
log_path = "/tmp/kele_watchdog.log"

# v2.10.10：T 状态超时阈值（秒）。超过这个时间还救不回来就升级到 SIGKILL。
T_KILL_AFTER = 30.0


def log(msg):
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
    sys.stderr.write(f"[{ts}] {msg}\n")


def get_pgid(pid: int):
    """拿 pid 所属的 process group id（拿不到返回 None）"""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "pgid=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return int(out) if out else None
    except Exception:
        return None


log(f"watchdog 启动：监控 pid={ffmpeg_pid} label={label}")

# 等 ffmpeg 进程出现（race condition 兜底：父进程启动 watchdog 时 ffmpeg 可能还没起来）
for _ in range(50):  # 最多等 5s
    try:
        os.kill(ffmpeg_pid, 0)
        break
    except ProcessLookupError:
        time.sleep(0.1)
    except Exception:
        break
else:
    log(f"5s 内 pid={ffmpeg_pid} 未出现，watchdog 退出")
    sys.exit(0)

# v2.10.10：拿 ffmpeg 的 PGID（升级到 PGID 级 SIGCONT 时用）
ffmpeg_pgid = get_pgid(ffmpeg_pid)
log(f"ffmpeg PGID={ffmpeg_pgid}（升级时 killpg 用）")

# v2.10.15：累计 T 状态总时长（不重置）+ 总运行时长兜底
# 之前 t_state_enter_time 在 ffmpeg 短暂退出 T 状态（被 SIGCONT 唤醒又被 SIGSTOP）时会被重置，
# 导致 SIGKILL 升级永远到不了 30s 阈值。改：累计每次进 T 状态的时长。
TOTAL_RUNTIME_HARD_LIMIT = 120.0  # ffmpeg 总运行超过 120s 强制 SIGKILL（兜底，不依赖状态）
ACCUM_T_KILL = 30.0               # T 状态累计 ≥30s 强制 SIGKILL（v2.10.29 从 15s 调宽，让 tessus 启动期不误杀）
ACCUM_T_WARN = 10.0                # T 状态累计 ≥10s 日志告警（v2.10.29 从 5s 调宽）

start_time = time.time()          # watchdog 启动时间
t_state_enter_time = None         # 本次进 T 状态时间戳（None=未在 T 状态）
accumulated_t_time = 0.0          # 累计 T 状态总时长（秒）

while True:
    time.sleep(5)

    # 一次性查 stat（同时承担"探活 + 拿状态"两个任务）
    try:
        stat = subprocess.check_output(
            ["ps", "-o", "stat=", "-p", str(ffmpeg_pid)],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        log(f"pid={ffmpeg_pid} 已退出，watchdog 退出")
        break
    except Exception as e:
        log(f"ps 异常 {e}")
        continue

    if not stat:
        log(f"pid={ffmpeg_pid} 已消失，watchdog 退出")
        break

    if stat.startswith("Z"):
        log(f"pid={ffmpeg_pid} 已成 zombie（stat={stat}），watchdog 退出")
        break

    # 兜底：ffmpeg 总运行超过硬上限 → 强制 SIGKILL
    total_runtime = time.time() - start_time
    if total_runtime > TOTAL_RUNTIME_HARD_LIMIT:
        log(f"⏰ ffmpeg 总运行 {total_runtime:.0f}s > 硬上限 {TOTAL_RUNTIME_HARD_LIMIT:.0f}s，强制 SIGKILL")
        try:
            os.kill(ffmpeg_pid, signal.SIGKILL)
            log(f"  → SIGKILL 成功")
        except ProcessLookupError:
            log(f"  → pid 在 SIGKILL 前已退出")
        except Exception as e:
            log(f"  → SIGKILL 失败 {type(e).__name__}: {e}")
        break

    if stat[0] == "T":
        # 首次进 T 状态 → 记时间戳
        if t_state_enter_time is None:
            t_state_enter_time = time.time()
            log(f"⚠️ pid={ffmpeg_pid} 进入 T 状态（累计 {accumulated_t_time:.1f}s）")

        # 本次 T 持续时间 + 累计 T 持续时间
        current_t_duration = time.time() - t_state_enter_time
        total_t = accumulated_t_time + current_t_duration

        if total_t < ACCUM_T_WARN:
            # 阶段 1：T 累计 < 5s，先 SIGCONT 试试
            try:
                os.kill(ffmpeg_pid, signal.SIGCONT)
                log(f"  → SIGCONT 单进程（累计 T {total_t:.1f}s / 警告阈值 {ACCUM_T_WARN:.0f}s）")
            except ProcessLookupError:
                log(f"  → pid 在 SIGCONT 前已退出")
                break
            except Exception as e:
                log(f"  → SIGCONT 失败 {type(e).__name__}: {e}")

            if ffmpeg_pgid and ffmpeg_pgid != ffmpeg_pid:
                try:
                    os.killpg(ffmpeg_pgid, signal.SIGCONT)
                    log(f"  → SIGCONT PGID={ffmpeg_pgid}")
                except ProcessLookupError:
                    pass
                except Exception as e:
                    log(f"  → killpg SIGCONT 失败 {type(e).__name__}: {e}")
        else:
            # 阶段 2：T 累计 ≥ 5s，macOS 上 SIGCONT 大概率无效，直接 SIGKILL
            log(f"  → T 累计 {total_t:.1f}s ≥ 警告阈值 {ACCUM_T_WARN:.0f}s，SIGCONT 在 macOS 对 T 无效，升级 SIGKILL")
            try:
                os.kill(ffmpeg_pid, signal.SIGKILL)
                log(f"  → SIGKILL 成功（让 ASR 那边拿到非 0 退出码走 except）")
            except ProcessLookupError:
                log(f"  → pid 在 SIGKILL 前已退出")
            except Exception as e:
                log(f"  → SIGKILL 失败 {type(e).__name__}: {e}")
            break
    else:
        # 不在 T 状态 → 把本次 T 持续时间加到累计
        if t_state_enter_time is not None:
            current_t_duration = time.time() - t_state_enter_time
            accumulated_t_time += current_t_duration
            log(f"✅ pid={ffmpeg_pid} 短暂恢复 stat={stat}（本次 T {current_t_duration:.1f}s，累计 {accumulated_t_time:.1f}s）")
            t_state_enter_time = None
            # 如果累计 T 已经超过警告阈值，下次进 T 立刻 SIGKILL
            # （不再重置累计，除非 ffmpeg 真的结束）
