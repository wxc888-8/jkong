import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from substrateinterface import SubstrateInterface

_dotenv_loaded = False
_dotenv_path = None
_dotenv_cache = {}
_dotenv_mtime = None
_dotenv_lock = threading.Lock()

CN_TZ = timezone(timedelta(hours=8))

def load_dotenv_if_present():
    global _dotenv_loaded
    global _dotenv_path
    if _dotenv_loaded:
        return
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        dotenv_path = os.path.join(base_dir, ".env")
    except Exception:
        dotenv_path = ".env"
    _dotenv_path = dotenv_path
    _dotenv_loaded = True

def parse_dotenv_dict(dotenv_path):
    out = {}
    if not dotenv_path:
        return out
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        if not key:
            continue
        val = v.strip()
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        out[key] = val
    return out

def refresh_dotenv_cache_if_needed():
    global _dotenv_mtime
    global _dotenv_cache
    load_dotenv_if_present()
    if not _dotenv_path:
        return
    try:
        mtime = os.path.getmtime(_dotenv_path)
    except Exception:
        return
    if _dotenv_mtime == mtime:
        return
    with _dotenv_lock:
        try:
            mtime2 = os.path.getmtime(_dotenv_path)
        except Exception:
            return
        if _dotenv_mtime == mtime2:
            return
        _dotenv_cache = parse_dotenv_dict(_dotenv_path)
        _dotenv_mtime = mtime2

def get_cfg_raw(name):
    load_dotenv_if_present()
    refresh_dotenv_cache_if_needed()
    if name in _dotenv_cache:
        return _dotenv_cache.get(name)
    return os.getenv(name)

def getenv_str(name, default=None):
    v = get_cfg_raw(name)
    if v is None:
        return default
    s = str(v).strip()
    return s if s != "" else default

def getenv_int(name, default):
    s = getenv_str(name, None)
    if s is None:
        return default
    try:
        return int(float(s))
    except Exception:
        return default

def getenv_float(name, default):
    s = getenv_str(name, None)
    if s is None:
        return default
    try:
        return float(s)
    except Exception:
        return default

def parse_int_set(s):
    out = set()
    if not s:
        return out
    for part in str(s).replace(";", ",").replace("|", ",").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except Exception:
            continue
    return out

def get_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    })
    return session

def tg_send_text(session, bot_token, chat_id, text):
    if not bot_token or chat_id is None:
        return False
    safe_text = "" if text is None else str(text)
    if len(safe_text) > 3900:
        safe_text = safe_text[:3900] + "\n…(truncated)"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": safe_text,
        "disable_web_page_preview": True
    }
    try:
        resp = session.post(url, json=payload, timeout=20)
        return resp.status_code == 200
    except Exception:
        return False

def tg_get_updates(session, bot_token, offset, timeout_seconds):
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {
        "timeout": int(timeout_seconds),
        "offset": int(offset) if offset is not None else 0,
        "allowed_updates": json.dumps(["message"])
    }
    try:
        resp = session.get(url, params=params, timeout=int(timeout_seconds) + 10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return []
        result = data.get("result", [])
        return result if isinstance(result, list) else []
    except Exception:
        return []

def now_cn_str():
    return datetime.now(timezone.utc).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

def get_db_path():
    return getenv_str("BOT2_DB_PATH", "/app/data/bot2.db")

def db_connect():
    path = get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def db_init(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS watches (
        chat_id INTEGER NOT NULL,
        address TEXT NOT NULL,
        remark TEXT,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (chat_id, address)
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS chat_settings (
        chat_id INTEGER PRIMARY KEY,
        events TEXT NOT NULL DEFAULT 'all'
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        chat_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL,
        data TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    );
    """)

def get_events_setting(conn, chat_id):
    try:
        row = conn.execute("SELECT events FROM chat_settings WHERE chat_id=?", (int(chat_id),)).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return "all"

def set_events_setting(conn, chat_id, events):
    e = (events or "all").strip() or "all"
    conn.execute("INSERT INTO chat_settings(chat_id, events) VALUES(?, ?) ON CONFLICT(chat_id) DO UPDATE SET events=excluded.events", (int(chat_id), e))

def get_watch_count(conn, chat_id):
    try:
        row = conn.execute("SELECT COUNT(1) FROM watches WHERE chat_id=?", (int(chat_id),)).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0

def add_watch(conn, chat_id, address, remark):
    conn.execute(
        "INSERT OR REPLACE INTO watches(chat_id, address, remark, created_at) VALUES(?, ?, ?, ?)",
        (int(chat_id), str(address), str(remark) if remark else None, int(time.time()))
    )

def remove_watch(conn, chat_id, address):
    conn.execute("DELETE FROM watches WHERE chat_id=? AND address=?", (int(chat_id), str(address)))

def list_watches(conn, chat_id, limit, offset):
    try:
        rows = conn.execute(
            "SELECT address, COALESCE(remark, ''), created_at FROM watches WHERE chat_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (int(chat_id), int(limit), int(offset))
        ).fetchall()
        return rows if isinstance(rows, list) else []
    except Exception:
        return []

def find_watches_by_address(conn, address):
    try:
        rows = conn.execute(
            "SELECT chat_id, COALESCE(remark, ''), created_at FROM watches WHERE address=?",
            (str(address),)
        ).fetchall()
        return rows if isinstance(rows, list) else []
    except Exception:
        return []

def get_conversation(conn, chat_id):
    try:
        row = conn.execute("SELECT state, data FROM conversations WHERE chat_id=?", (int(chat_id),)).fetchone()
        if row and row[0] and row[1] is not None:
            return (str(row[0]), str(row[1]))
    except Exception:
        pass
    return None

def set_conversation(conn, chat_id, state, data_obj):
    data_s = json.dumps(data_obj, ensure_ascii=False)
    conn.execute(
        "INSERT INTO conversations(chat_id, state, data, updated_at) VALUES(?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, data=excluded.data, updated_at=excluded.updated_at",
        (int(chat_id), str(state), data_s, int(time.time()))
    )

def clear_conversation(conn, chat_id):
    conn.execute("DELETE FROM conversations WHERE chat_id=?", (int(chat_id),))

ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{44,64}")

def extract_address(text):
    if not isinstance(text, str):
        return None
    m = ADDRESS_RE.search(text.strip())
    return m.group(0) if m else None

def get_limits(user_id):
    admins = parse_int_set(getenv_str("BOT2_TG_ADMIN_USER_IDS", ""))
    if int(user_id) in admins:
        return ("admin", 3001)
    return ("free", 3)

def cmd_help():
    return "\n".join([
        "🤖 Bittensor 地址监控机器人",
        "",
        "常用命令：",
        "/watch 添加监听地址（交互式）",
        "/batchadd 批量添加地址",
        "/unwatch 删除监听地址（交互式）",
        "/batchremove 批量删除地址",
        "/remark <地址> <新备注> 更新备注",
        "/list [页码] 查看监听列表（默认第1页）",
        "/setevents 设置监听事件类型",
        "/status 查看当前状态",
        "/contact 联系管理员",
        "/cancel 取消当前操作",
        "/help 显示本帮助",
        "",
        "提示：",
        "先发 /whoami 获取你的用户ID；管理员ID配置在服务器 .env 的 BOT2_TG_ADMIN_USER_IDS",
    ])

def cmd_contact():
    s = getenv_str("BOT2_CONTACT", "")
    if s:
        return s
    return "请联系管理员。"

def handle_command(conn, session, token, msg):
    chat = msg.get("chat", {}) if isinstance(msg.get("chat"), dict) else {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    from_user = msg.get("from", {}) if isinstance(msg.get("from"), dict) else {}
    user_id = from_user.get("id")
    text = msg.get("text")
    if chat_id is None or user_id is None:
        return

    is_private = (chat_type == "private")
    cmd = (text or "").strip() if isinstance(text, str) else ""

    if cmd.startswith("/help"):
        tg_send_text(session, token, chat_id, cmd_help())
        return

    if cmd.startswith("/whoami"):
        name = (from_user.get("username") or from_user.get("first_name") or "").strip()
        tg_send_text(session, token, chat_id, "\n".join([
            "你的 Telegram 用户 ID：",
            str(user_id),
            f"用户名：{name}" if name else "用户名：N/A",
        ]))
        return

    if cmd.startswith("/contact"):
        tg_send_text(session, token, chat_id, cmd_contact())
        return

    if cmd.startswith("/cancel"):
        clear_conversation(conn, chat_id)
        tg_send_text(session, token, chat_id, "已取消。")
        return

    conv = get_conversation(conn, chat_id)
    if conv and (not cmd.startswith("/")):
        state, data_s = conv
        try:
            data = json.loads(data_s) if data_s else {}
        except Exception:
            data = {}
        if state == "watch_wait_address":
            addr = extract_address(cmd)
            if not addr:
                tg_send_text(session, token, chat_id, "没识别到地址，请再发一次地址。")
                return
            tier, limit = get_limits(user_id)
            cnt = get_watch_count(conn, chat_id)
            if cnt >= limit:
                tg_send_text(session, token, chat_id, f"已达到上限：{cnt}/{limit}（{tier}）")
                clear_conversation(conn, chat_id)
                return
            data["address"] = addr
            set_conversation(conn, chat_id, "watch_wait_remark", data)
            tg_send_text(session, token, chat_id, "已收到地址。请发送备注（可直接发“无”跳过）。")
            return
        if state == "watch_wait_remark":
            addr = data.get("address")
            remark = cmd.strip()
            if remark in ("无", "不需要", "skip", "SKIP", "-"):
                remark = ""
            if addr:
                add_watch(conn, chat_id, addr, remark)
                clear_conversation(conn, chat_id)
                tg_send_text(session, token, chat_id, f"已添加监听：{addr}" + (f"\n备注：{remark}" if remark else ""))
            else:
                clear_conversation(conn, chat_id)
                tg_send_text(session, token, chat_id, "操作已重置，请重新 /watch。")
            return
        if state == "unwatch_wait_address":
            addr = extract_address(cmd)
            if not addr:
                tg_send_text(session, token, chat_id, "没识别到地址，请再发一次要删除的地址。")
                return
            remove_watch(conn, chat_id, addr)
            clear_conversation(conn, chat_id)
            tg_send_text(session, token, chat_id, f"已删除：{addr}")
            return
        if state == "batchadd_wait_lines":
            lines = [ln.strip() for ln in cmd.splitlines() if ln.strip()]
            if not lines:
                tg_send_text(session, token, chat_id, "空输入，已取消。")
                clear_conversation(conn, chat_id)
                return
            tier, limit = get_limits(user_id)
            cnt = get_watch_count(conn, chat_id)
            added = 0
            for ln in lines:
                addr = extract_address(ln)
                if not addr:
                    continue
                if cnt + added >= limit:
                    break
                remark = ln.replace(addr, "").strip()
                add_watch(conn, chat_id, addr, remark)
                added += 1
            clear_conversation(conn, chat_id)
            tg_send_text(session, token, chat_id, f"批量添加完成：新增 {added} 个（上限 {limit}）")
            return
        if state == "batchremove_wait_lines":
            lines = [ln.strip() for ln in cmd.splitlines() if ln.strip()]
            if not lines:
                tg_send_text(session, token, chat_id, "空输入，已取消。")
                clear_conversation(conn, chat_id)
                return
            removed = 0
            for ln in lines:
                addr = extract_address(ln)
                if not addr:
                    continue
                remove_watch(conn, chat_id, addr)
                removed += 1
            clear_conversation(conn, chat_id)
            tg_send_text(session, token, chat_id, f"批量删除完成：{removed} 个")
            return
        if state == "setevents_wait_choice":
            choice = cmd.strip().lower()
            mapping = {
                "all": "all",
                "transfer": "transfer",
                "stake": "stake",
                "unstake": "unstake",
            }
            if choice not in mapping:
                tg_send_text(session, token, chat_id, "可选：all / transfer / stake / unstake")
                return
            set_events_setting(conn, chat_id, mapping[choice])
            clear_conversation(conn, chat_id)
            tg_send_text(session, token, chat_id, f"已设置事件：{mapping[choice]}")
            return

    if cmd.startswith("/watch"):
        if not is_private:
            pass
        set_conversation(conn, chat_id, "watch_wait_address", {})
        tg_send_text(session, token, chat_id, "请发送要监听的地址。")
        return

    if cmd.startswith("/batchadd"):
        set_conversation(conn, chat_id, "batchadd_wait_lines", {})
        tg_send_text(session, token, chat_id, "请一次发送多行地址（可在地址后面加备注）。发送 /cancel 取消。")
        return

    if cmd.startswith("/unwatch"):
        set_conversation(conn, chat_id, "unwatch_wait_address", {})
        tg_send_text(session, token, chat_id, "请发送要删除的地址。")
        return

    if cmd.startswith("/batchremove"):
        set_conversation(conn, chat_id, "batchremove_wait_lines", {})
        tg_send_text(session, token, chat_id, "请一次发送多行要删除的地址。发送 /cancel 取消。")
        return

    if cmd.startswith("/remark"):
        parts = cmd.split(None, 2)
        if len(parts) < 3:
            tg_send_text(session, token, chat_id, "用法：/remark <地址> <新备注>")
            return
        addr = extract_address(parts[1])
        remark = parts[2].strip()
        if not addr:
            tg_send_text(session, token, chat_id, "地址格式不对。")
            return
        add_watch(conn, chat_id, addr, remark)
        tg_send_text(session, token, chat_id, f"已更新备注：{addr}\n备注：{remark}")
        return

    if cmd.startswith("/list"):
        page = 1
        parts = cmd.split(None, 1)
        if len(parts) == 2:
            try:
                page = max(1, int(parts[1].strip()))
            except Exception:
                page = 1
        page_size = getenv_int("BOT2_PAGE_SIZE", 10)
        offset = (page - 1) * page_size
        rows = list_watches(conn, chat_id, page_size, offset)
        tier, limit = get_limits(user_id)
        cnt = get_watch_count(conn, chat_id)
        if not rows:
            tg_send_text(session, token, chat_id, f"监听列表为空（{cnt}/{limit}）。用 /watch 添加。")
            return
        lines = [f"📋 监听列表 第{page}页（{cnt}/{limit} {tier}）"]
        for i, (addr, remark, created_at) in enumerate(rows, start=1 + offset):
            ts = datetime.fromtimestamp(int(created_at), tz=timezone.utc).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if remark:
                lines.append(f"{i}. {addr}  [{remark}]  {ts}")
            else:
                lines.append(f"{i}. {addr}  {ts}")
        tg_send_text(session, token, chat_id, "\n".join(lines))
        return

    if cmd.startswith("/setevents"):
        set_conversation(conn, chat_id, "setevents_wait_choice", {})
        current = get_events_setting(conn, chat_id)
        tg_send_text(session, token, chat_id, "\n".join([
            f"当前事件：{current}",
            "请选择：all / transfer / stake / unstake",
        ]))
        return

    if cmd.startswith("/status"):
        tier, limit = get_limits(user_id)
        cnt = get_watch_count(conn, chat_id)
        events = get_events_setting(conn, chat_id)
        tg_send_text(session, token, chat_id, "\n".join([
            f"时间：{now_cn_str()}",
            f"聊天ID：{chat_id}",
            f"用户ID：{user_id}",
            f"等级：{tier}",
            f"地址数：{cnt}/{limit}",
            f"事件：{events}",
        ]))
        return

    if cmd.startswith("/price") or cmd.startswith("/stakes") or cmd.startswith("/query") or cmd.startswith("/balance") or cmd.startswith("/hold") or cmd.startswith("/holdall") or cmd.startswith("/balances"):
        tg_send_text(session, token, chat_id, "该命令暂未实现。")
        return

def classify_event_type(call_module, call_function):
    m = (call_module or "").lower()
    f = (call_function or "").lower()
    if m == "balances" and f in ("transfer", "transfer_keep_alive"):
        return "transfer"
    if m == "subtensormodule" and f.startswith("add_stake"):
        return "stake"
    if m == "subtensormodule" and f.startswith("remove_stake"):
        return "unstake"
    return "other"

def start_chain_monitor(conn, session, token):
    wss_url = getenv_str("BOT2_SUBSTRATE_WSS_URL", getenv_str("SUBSTRATE_WSS_URL", "wss://entrypoint-finney.opentensor.ai:443"))
    substrate = SubstrateInterface(url=wss_url)
    substrate.init_runtime()

    def on_block(obj, update_nr, sub_id):
        try:
            header = obj.get("header", {})
            block_number = header.get("number")
            if block_number is None:
                return None
            block_hash = substrate.get_block_hash(block_number)
            block = substrate.get_block(block_hash=block_hash)
            extrinsics = block.get("extrinsics", [])
            for ex in extrinsics:
                try:
                    exv = ex.value
                except Exception:
                    continue
                signer = exv.get("address")
                if not signer:
                    continue
                call = exv.get("call", {})
                if not isinstance(call, dict):
                    continue
                call_module = call.get("call_module")
                call_function = call.get("call_function")
                ev_type = classify_event_type(call_module, call_function)

                watchers = find_watches_by_address(conn, signer)
                if not watchers:
                    continue
                for (chat_id, remark, _) in watchers:
                    wanted = get_events_setting(conn, chat_id)
                    if wanted != "all" and wanted != ev_type:
                        continue
                    tx_hash = exv.get("extrinsic_hash", "")
                    title = "📌 地址事件通知"
                    lines = [
                        title,
                        f"时间：{now_cn_str()}",
                        f"地址：{signer}" + (f"（{remark}）" if remark else ""),
                        f"事件：{call_module}.{call_function}",
                    ]
                    if tx_hash:
                        lines.append(f"哈希：{tx_hash}")
                    tg_send_text(session, token, chat_id, "\n".join(lines))
        except Exception:
            return None
        return None

    substrate.subscribe_block_headers(on_block)

def main():
    token = getenv_str("BOT2_TELEGRAM_BOT_TOKEN", None)
    if not token:
        print("BOT2_TELEGRAM_BOT_TOKEN 未配置")
        return
    poll_timeout = getenv_int("BOT2_POLL_TIMEOUT", 30)
    session = get_session()
    conn = db_connect()
    db_init(conn)

    t = threading.Thread(target=start_chain_monitor, args=(conn, session, token), daemon=True)
    t.start()

    offset = 0
    while True:
        updates = tg_get_updates(session, token, offset, poll_timeout)
        for upd in updates:
            if not isinstance(upd, dict):
                continue
            uid = upd.get("update_id")
            if isinstance(uid, int):
                offset = max(offset, uid + 1)
            msg = upd.get("message")
            if isinstance(msg, dict):
                handle_command(conn, session, token, msg)
        time.sleep(1)

if __name__ == "__main__":
    main()
