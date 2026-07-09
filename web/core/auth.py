"""
web/core/auth.py - v1.29 会员系统预留接口

【当前状态】占位实现：所有访问默认是 guest 用户（id=1）
【未来对接】卡密登录（来自玄学 App http://14.116.211.42:10003/admin），
              用户激活后获得"会员身份"，人设/预设/历史都是各自的。

数据库表（已建）：
  users(id, username, password_hash, card_key, member_level, expires_at, created_at)
  presets(user_id, persona_name, image_path, audio_path, default_action_prompt, created_at)

调用方约定：
  from web.core.auth import current_user, login_required, get_user_presets
  @login_required   ← 接卡密登录后这装饰器会真正校验
  def my_route(): user = current_user()   ← 现在返回 guest dict
"""

from __future__ import annotations

import functools
import sqlite3
import time
from pathlib import Path
from typing import Optional


# ===== 数据库 =====
DB_PATH = Path("/tmp/kele_web.db")


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """启动时调用，建表 + 确保 guest 用户存在"""
    conn = _get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT,
        card_key TEXT,
        member_level TEXT DEFAULT 'free',
        expires_at INTEGER DEFAULT 0,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        persona_name TEXT NOT NULL,
        image_path TEXT,
        audio_path TEXT,
        default_action_prompt TEXT DEFAULT '',
        created_at INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(user_id, persona_name)
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        kind TEXT NOT NULL,
        log_json TEXT NOT NULL DEFAULT '[]',
        error TEXT,
        result_path TEXT,
        created_at INTEGER NOT NULL
    );
    """)
    # 确保 guest 用户存在（v1.29 单租户时期，所有请求归他）
    c.execute("SELECT id FROM users WHERE username = 'guest'")
    row = c.fetchone()
    if not row:
        c.execute(
            "INSERT INTO users (username, password_hash, member_level, created_at) VALUES (?, ?, ?, ?)",
            ("guest", "", "free", int(time.time())),
        )
    conn.commit()
    conn.close()


# ===== 跨 worker 任务共享（SQLite 替代内存 dict）=====
def task_create(task_id: str, kind: str, initial_log: list) -> None:
    import json as _json
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tasks (id, status, kind, log_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, "processing", kind, _json.dumps(initial_log, ensure_ascii=False), int(time.time())),
    )
    conn.commit()
    conn.close()


def task_get(task_id: str) -> dict | None:
    import json as _json
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, status, kind, log_json, error, result_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["log"] = _json.loads(d.pop("log_json") or "[]")
    return d


def task_append_log(task_id: str, msg: str) -> None:
    """追加一条日志（线程安全，SQLite 写串行）"""
    import json as _json
    conn = _get_conn()
    row = conn.execute("SELECT log_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return
    logs = _json.loads(row["log_json"] or "[]")
    logs.append(msg)
    conn.execute(
        "UPDATE tasks SET log_json = ? WHERE id = ?",
        (_json.dumps(logs, ensure_ascii=False), task_id),
    )
    conn.commit()
    conn.close()


def task_finish(task_id: str, status: str, error: str = None, result_path: str = None) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE tasks SET status = ?, error = ?, result_path = ? WHERE id = ?",
        (status, error, result_path, task_id),
    )
    conn.commit()
    conn.close()


def task_count_active() -> int:
    conn = _get_conn()
    n = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'processing'").fetchone()[0]
    conn.close()
    return n


def task_cleanup(ttl_seconds: int = 600) -> int:
    """清理 ttl_seconds 之前创建的已完成/失败任务，返回清理数"""
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM tasks WHERE status != 'processing' AND created_at < ?",
        (int(time.time()) - ttl_seconds,),
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


# ===== 当前用户（占位：永远返回 guest）=====
def current_user() -> dict:
    """返回当前请求关联的用户。v1.29 永远是 guest。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, username, member_level, expires_at FROM users WHERE username='guest'"
    ).fetchone()
    conn.close()
    return dict(row) if row else {"id": 1, "username": "guest", "member_level": "free"}


def login_required(f):
    """装饰器：v1.29 永远放行。未来接卡密登录后这里校验 session/cookie"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        # 未来这里检查 user['member_level'] 和 expires_at
        # if user['member_level'] == 'free' and 路径需要会员: abort(403)
        return f(*args, **kwargs)
    return wrapper


# ===== 预设 CRUD（v1.29 是空操作，未来按 user_id 过滤）=====
def get_user_presets(user_id: int) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, persona_name, image_path, audio_path, default_action_prompt "
        "FROM presets WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_preset(user_id: int, persona_name: str, image_path: str = "", audio_path: str = "", action_prompt: str = "") -> int:
    conn = _get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO presets (user_id, persona_name, image_path, audio_path, default_action_prompt, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, persona_name, image_path, audio_path, action_prompt, int(time.time())),
    )
    preset_id = c.lastrowid
    conn.commit()
    conn.close()
    return preset_id


def delete_preset(user_id: int, preset_id: int) -> bool:
    conn = _get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM presets WHERE id = ? AND user_id = ?", (preset_id, user_id))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# ===== 未来对接玄学 App 卡密的钩子（占位）=====
def verify_card_key(card_key: str) -> Optional[dict]:
    """
    验证卡密。v1.29 永远返回 None（未实现）。
    未来这里调用 http://14.116.211.42:10003/admin/api/verify-card 验证卡密，
    返回 {"valid": True, "member_level": "month"/"year", "expires_at": ...} 或 None。
    """
    return None