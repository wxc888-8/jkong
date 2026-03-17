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
_tao_price_cache = {"ts": 0.0, "usd": None}
_alpha_price_cache = {"ts": 0.0, "map": {}}
_substrate_lock = threading.Lock()
_substrate = None

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

def tg_send_text(session, bot_token, chat_id, text, parse_mode=None):
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
    if parse_mode:
        payload["parse_mode"] = str(parse_mode)
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
    members = parse_int_set(getenv_str("BOT2_TG_MEMBER_USER_IDS", ""))
    if int(user_id) in members:
        return ("member", 3000)
    return ("free", 3)

def cmd_help():
    return "\n".join([
        "🤖 *Bittensor 地址监控机器人*",
        "",
        "📋 *地址管理*",
        f"{md_code('/watch')} 添加监听地址（交互式）",
        f"{md_code('/batchadd')} 批量添加地址（多行，地址后可带备注）",
        f"{md_code('/unwatch')} 删除监听地址（交互式）",
        f"{md_code('/batchremove')} 批量删除地址（多行）",
        f"{md_code('/remark <地址> <新备注>')} 更新备注",
        f"{md_code('/list [页码]')} 查看监听列表（默认第1页）",
        "",
        "⚙️ *监听设置*",
        f"{md_code('/setevents')} 设置监听事件类型（all/transfer/stake/unstake）",
        "",
        "🔍 *查询/统计*",
        f"{md_code('/price')} 查看 TAO/USD 价格",
        f"{md_code('/balance')} 余额查询（可回复包含地址的消息）",
        f"{md_code('/query <地址>')} 地址信息（余额 + 子网代币）",
        f"{md_code('/stakes <地址>')} 子网代币（支持回复消息）",
        f"{md_code('/hold <子网ID>')} 当前聊天监听地址在该子网持有排行",
        f"{md_code('/holdall')} 各子网持有量汇总（当前聊天）",
        f"{md_code('/balances [available|total|f]')} 当前聊天资产汇总",
        "",
        "ℹ️ *其他*",
        f"{md_code('/status')} 查看当前状态",
        f"{md_code('/contact')} 联系管理员",
        f"{md_code('/cancel')} 取消当前操作",
        f"{md_code('/whoami')} 获取你的用户ID",
        f"{md_code('/help')} 显示本帮助",
        "",
        "💡 *提示*",
        f"统计命令（{md_code('/hold')} / {md_code('/holdall')} / {md_code('/balances')}）基于“当前聊天”的监听列表，请先用 /watch 添加地址。",
        "管理员/会员在服务器 .env 配置：BOT2_TG_ADMIN_USER_IDS / BOT2_TG_MEMBER_USER_IDS",
    ])

def cmd_contact():
    s = getenv_str("BOT2_CONTACT", "")
    if s:
        return s
    return "请联系管理员。"

def get_tao_price_usd(session):
    now = time.time()
    if now - float(_tao_price_cache.get("ts") or 0.0) < 20 and _tao_price_cache.get("usd") is not None:
        return _tao_price_cache.get("usd")
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        usd = None
        if isinstance(data, dict):
            usd = ((data.get("bittensor") or {}).get("usd")) if isinstance(data.get("bittensor"), dict) else None
        usd_f = float(usd) if usd is not None else None
        _tao_price_cache["ts"] = now
        _tao_price_cache["usd"] = usd_f
        return usd_f
    except Exception:
        return _tao_price_cache.get("usd")

def get_alpha_price_map(session):
    now = time.time()
    if now - float(_alpha_price_cache.get("ts") or 0.0) < 60 and _alpha_price_cache.get("map"):
        return _alpha_price_cache.get("map") or {}
    url = "https://taostats.io/api/dtao/dtaoSubnets?limit=500&order=netuid_asc"
    out = {}
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", []) if isinstance(data, dict) else []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                netuid = row.get("netuid")
                price = row.get("price")
                if netuid is None or price is None:
                    continue
                try:
                    out[int(netuid)] = float(price)
                except Exception:
                    continue
        _alpha_price_cache["ts"] = now
        _alpha_price_cache["map"] = out
        return out
    except Exception:
        return _alpha_price_cache.get("map") or {}

def get_substrate():
    global _substrate
    wss_url = getenv_str("BOT2_SUBSTRATE_WSS_URL", getenv_str("SUBSTRATE_WSS_URL", "wss://entrypoint-finney.opentensor.ai:443"))
    with _substrate_lock:
        if _substrate is not None:
            return _substrate
        s = SubstrateInterface(url=wss_url)
        s.init_runtime()
        _substrate = s
        return _substrate

def tao_from_rao(v):
    try:
        return float(v) / 1_000_000_000
    except Exception:
        return 0.0

def get_system_balance_tao(address):
    s = get_substrate()
    try:
        acc = s.query("System", "Account", [address]).value
        data = acc.get("data", {}) if isinstance(acc, dict) else {}
        free = tao_from_rao(data.get("free", 0))
        reserved = tao_from_rao(data.get("reserved", 0))
        misc_frozen = tao_from_rao(data.get("misc_frozen", 0))
        fee_frozen = tao_from_rao(data.get("fee_frozen", 0))
        return {"free": free, "reserved": reserved, "misc_frozen": misc_frozen, "fee_frozen": fee_frozen}
    except Exception:
        return {"free": 0.0, "reserved": 0.0, "misc_frozen": 0.0, "fee_frozen": 0.0}

def get_staking_hotkeys(coldkey):
    s = get_substrate()
    try:
        v = s.query("SubtensorModule", "StakingHotkeys", [coldkey]).value
        return v if isinstance(v, list) else []
    except Exception:
        return []

def get_hotkey_alpha_map(hotkey):
    s = get_substrate()
    out = {}
    try:
        it = s.query_map("SubtensorModule", "TotalHotkeyAlpha", [hotkey])
        for k, v in it:
            try:
                netuid = int(k.value)
            except Exception:
                continue
            out[netuid] = int(v.value) if v.value is not None else 0
    except Exception:
        return {}
    return out

def get_coldkey_alpha_summary(coldkey):
    hotkeys = get_staking_hotkeys(coldkey)
    totals = {}
    for hk in hotkeys:
        hk_map = get_hotkey_alpha_map(hk)
        for netuid, alpha_rao in hk_map.items():
            totals[netuid] = totals.get(netuid, 0) + int(alpha_rao or 0)
    return totals

def fmt_money(v):
    try:
        return f"{float(v):,.4f}"
    except Exception:
        return str(v)

def fmt_num(v, digits=3):
    try:
        return f"{float(v):,.{int(digits)}f}"
    except Exception:
        return str(v)

def sanitize_md_code(s):
    return str(s or "").replace("`", "'")

def md_code(s):
    return f"`{sanitize_md_code(s)}`"

def md_bold(s):
    return f"*{str(s or '')}*"

def send_md(session, token, chat_id, text):
    return tg_send_text(session, token, chat_id, text, parse_mode="Markdown")

def short_addr(addr, head=4, tail=4):
    s = str(addr or "")
    if len(s) <= head + tail + 2:
        return s
    return s[:head] + ".." + s[-tail:]

def taostats_account_url(addr):
    a = str(addr or "").strip()
    if not a:
        return "https://taostats.io/"
    return f"https://taostats.io/account/{a}"

def build_address_report_markdown(address, free_tao, subnet_tao_items, tao_usd):
    a_short = short_addr(address)
    url = taostats_account_url(address)
    lines = [
        f"💰 {md_bold('地址信息')}",
        f"📍 {md_code(a_short)} ([taostats]({url}))",
        "",
        f"🌐 {md_bold('子网代币')}",
    ]
    if subnet_tao_items:
        for netuid, tao_equiv in subnet_tao_items:
            lines.append(f"🌠SN{netuid}:{fmt_num(tao_equiv, 3)} 𝞃")
    else:
        lines.append("（暂无子网持有或暂时获取不到价格）")

    usd_part = ""
    if tao_usd:
        usd_part = f" (${fmt_num(free_tao * tao_usd, 2)})"
    lines.extend([
        "",
        f"💰 💸 可用余额(free): {fmt_num(free_tao, 3)}𝞃{usd_part}",
    ])
    return "\n".join(lines)

def extract_address_from_message(msg):
    if not isinstance(msg, dict):
        return None
    txt = msg.get("text")
    if isinstance(txt, str):
        a = extract_address(txt)
        if a:
            return a
    rep = msg.get("reply_to_message")
    if isinstance(rep, dict):
        t2 = rep.get("text")
        if isinstance(t2, str):
            return extract_address(t2)
    return None

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
        send_md(session, token, chat_id, cmd_help())
        return

    if cmd.startswith("/whoami"):
        name = (from_user.get("username") or from_user.get("first_name") or "").strip()
        send_md(session, token, chat_id, "\n".join([
            "👤 *你的信息*",
            f"用户ID：{md_code(user_id)}",
            f"用户名：{md_code(name) if name else 'N/A'}",
        ]))
        return

    if cmd.startswith("/contact"):
        send_md(session, token, chat_id, "\n".join([
            "☎️ *联系管理员*",
            str(cmd_contact() or "").strip(),
        ]))
        return

    if cmd.startswith("/cancel"):
        clear_conversation(conn, chat_id)
        send_md(session, token, chat_id, "✅ 已取消当前操作。")
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
                send_md(session, token, chat_id, "❌ 没识别到地址，请再发一次地址。")
                return
            tier, limit = get_limits(user_id)
            cnt = get_watch_count(conn, chat_id)
            if cnt >= limit:
                send_md(session, token, chat_id, f"⚠️ 已达到上限：{md_code(cnt)}/{md_code(limit)}（{tier}）")
                clear_conversation(conn, chat_id)
                return
            data["address"] = addr
            set_conversation(conn, chat_id, "watch_wait_remark", data)
            send_md(session, token, chat_id, "\n".join([
                "✅ 已收到地址。",
                "请发送备注（可直接发“无”跳过）。",
            ]))
            return
        if state == "watch_wait_remark":
            addr = data.get("address")
            remark = cmd.strip()
            if remark in ("无", "不需要", "skip", "SKIP", "-"):
                remark = ""
            if addr:
                add_watch(conn, chat_id, addr, remark)
                clear_conversation(conn, chat_id)
                send_md(session, token, chat_id, "\n".join([
                    "✅ 已添加监听",
                    f"地址：{md_code(short_addr(addr))}",
                    f"备注：{md_code(remark) if remark else '（无）'}",
                ]))
            else:
                clear_conversation(conn, chat_id)
                send_md(session, token, chat_id, f"⚠️ 操作已重置，请重新发送 {md_code('/watch')}。")
            return
        if state == "unwatch_wait_address":
            addr = extract_address(cmd)
            if not addr:
                send_md(session, token, chat_id, "❌ 没识别到地址，请再发一次要删除的地址。")
                return
            remove_watch(conn, chat_id, addr)
            clear_conversation(conn, chat_id)
            send_md(session, token, chat_id, f"✅ 已删除：{md_code(short_addr(addr))}")
            return
        if state == "batchadd_wait_lines":
            lines = [ln.strip() for ln in cmd.splitlines() if ln.strip()]
            if not lines:
                send_md(session, token, chat_id, "✅ 已取消（空输入）。")
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
            send_md(session, token, chat_id, f"✅ 批量添加完成：新增 {md_code(added)} 个（上限 {md_code(limit)}）")
            return
        if state == "batchremove_wait_lines":
            lines = [ln.strip() for ln in cmd.splitlines() if ln.strip()]
            if not lines:
                send_md(session, token, chat_id, "✅ 已取消（空输入）。")
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
            send_md(session, token, chat_id, f"✅ 批量删除完成：删除 {md_code(removed)} 个")
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
                send_md(session, token, chat_id, "可选：`all` / `transfer` / `stake` / `unstake`")
                return
            set_events_setting(conn, chat_id, mapping[choice])
            clear_conversation(conn, chat_id)
            name_map = {"all": "全部", "transfer": "转账", "stake": "质押", "unstake": "解押"}
            send_md(session, token, chat_id, f"✅ 已设置监听事件：{md_bold(name_map.get(mapping[choice], mapping[choice]))}")
            return

    if cmd.startswith("/watch"):
        if not is_private:
            pass
        set_conversation(conn, chat_id, "watch_wait_address", {})
        send_md(session, token, chat_id, "\n".join([
            "请发送要监听的地址。",
            f"发送 {md_code('/cancel')} 可取消。",
        ]))
        return

    if cmd.startswith("/batchadd"):
        set_conversation(conn, chat_id, "batchadd_wait_lines", {})
        send_md(session, token, chat_id, "\n".join([
            "请一次发送多行地址（可在地址后面加备注）。",
            "格式示例：",
            "5xxx...xxx  主钱包",
            "5yyy...yyy  冷钱包",
            f"发送 {md_code('/cancel')} 取消。",
        ]))
        return

    if cmd.startswith("/unwatch"):
        set_conversation(conn, chat_id, "unwatch_wait_address", {})
        send_md(session, token, chat_id, "\n".join([
            "请发送要删除的地址。",
            f"发送 {md_code('/cancel')} 取消。",
        ]))
        return

    if cmd.startswith("/batchremove"):
        set_conversation(conn, chat_id, "batchremove_wait_lines", {})
        send_md(session, token, chat_id, "\n".join([
            "请一次发送多行要删除的地址。",
            f"发送 {md_code('/cancel')} 取消。",
        ]))
        return

    if cmd.startswith("/remark"):
        parts = cmd.split(None, 2)
        if len(parts) < 3:
            send_md(session, token, chat_id, f"用法：{md_code('/remark <地址> <新备注>')}")
            return
        addr = extract_address(parts[1])
        remark = parts[2].strip()
        if not addr:
            send_md(session, token, chat_id, "❌ 地址格式不对。")
            return
        add_watch(conn, chat_id, addr, remark)
        send_md(session, token, chat_id, "\n".join([
            "✅ 已更新备注",
            f"地址：{md_code(short_addr(addr))}",
            f"备注：{md_code(remark) if remark else '（无）'}",
        ]))
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
            send_md(session, token, chat_id, f"📭 监听列表为空（{md_code(cnt)}/{md_code(limit)}）。用 {md_code('/watch')} 添加。")
            return
        lines = [f"📋 *监听列表* 第{md_code(page)}页（{md_code(cnt)}/{md_code(limit)} {tier}）"]
        for i, (addr, remark, created_at) in enumerate(rows, start=1 + offset):
            ts = datetime.fromtimestamp(int(created_at), tz=timezone.utc).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if remark:
                lines.append(f"{md_code(i)}. {md_code(short_addr(addr))}  {md_code(remark)}")
            else:
                lines.append(f"{md_code(i)}. {md_code(short_addr(addr))}")
        lines.append("")
        lines.append(f"提示：用 {md_code('/list 2')} 翻页；用 {md_code('/remark <地址> <备注>')} 修改备注。")
        send_md(session, token, chat_id, "\n".join(lines))
        return

    if cmd.startswith("/setevents"):
        set_conversation(conn, chat_id, "setevents_wait_choice", {})
        current = get_events_setting(conn, chat_id)
        name_map = {"all": "全部", "transfer": "转账", "stake": "质押", "unstake": "解押"}
        send_md(session, token, chat_id, "\n".join([
            "⚙️ *设置监听事件*",
            f"当前：{md_bold(name_map.get(current, current))}",
            "",
            "请选择发送：`all` / `transfer` / `stake` / `unstake`",
        ]))
        return

    if cmd.startswith("/status"):
        tier, limit = get_limits(user_id)
        cnt = get_watch_count(conn, chat_id)
        events = get_events_setting(conn, chat_id)
        price_usd = get_tao_price_usd(session)
        name_map = {"all": "全部", "transfer": "转账", "stake": "质押", "unstake": "解押"}
        send_md(session, token, chat_id, "\n".join([
            "📊 *状态*",
            f"时间：{md_code(now_cn_str())}",
            f"等级：{md_code(tier)}",
            f"地址数：{md_code(cnt)}/{md_code(limit)}",
            f"事件：{md_bold(name_map.get(events, events))}",
            f"TAO/USD：{md_code(fmt_money(price_usd) if price_usd else 'N/A')}",
        ]))
        return

    if cmd.startswith("/price"):
        usd = get_tao_price_usd(session)
        if usd is None:
            send_md(session, token, chat_id, "暂时获取不到价格。")
            return
        send_md(session, token, chat_id, "\n".join([
            "💱 *TAO 价格*",
            f"TAO/USD：{md_code(fmt_money(usd))}",
            f"时间：{md_code(now_cn_str())}",
        ]))
        return

    if cmd.startswith("/balance"):
        addr = extract_address_from_message(msg)
        if not addr:
            parts = cmd.split(None, 1)
            addr = extract_address(parts[1]) if len(parts) == 2 else None
        if not addr:
            tg_send_text(session, token, chat_id, "用法：回复一条包含地址的消息再发 /balance，或 /balance <地址>")
            return
        bal = get_system_balance_tao(addr)
        usd = get_tao_price_usd(session)
        msg_text = build_address_report_markdown(
            addr,
            bal["free"],
            [],
            usd,
        )
        tg_send_text(session, token, chat_id, msg_text, parse_mode="Markdown")
        return

    if cmd.startswith("/stakes") or cmd.startswith("/query"):
        parts = cmd.split(None, 1)
        addr = extract_address(parts[1]) if len(parts) == 2 else extract_address_from_message(msg)
        if not addr:
            tg_send_text(session, token, chat_id, "用法：/query <地址> 或 /stakes <地址>，也支持回复包含地址的消息。")
            return
        bal = get_system_balance_tao(addr)
        alpha_prices = get_alpha_price_map(session)
        alpha_by_netuid = get_coldkey_alpha_summary(addr)
        usd = get_tao_price_usd(session)

        subnet_items = []
        for netuid, alpha_rao in alpha_by_netuid.items():
            try:
                n = int(netuid)
            except Exception:
                continue
            price = alpha_prices.get(n)
            if price is None:
                continue
            alpha = tao_from_rao(alpha_rao)
            tao_equiv = alpha * float(price)
            subnet_items.append((n, tao_equiv))
        subnet_items.sort(key=lambda x: x[1], reverse=True)
        subnet_items = subnet_items[:12]

        msg_text = build_address_report_markdown(addr, bal["free"], subnet_items, usd)
        if cmd.startswith("/stakes"):
            msg_text = msg_text.replace("💰 *地址信息*", "📊 *子网代币*")
        send_md(session, token, chat_id, msg_text)
        return

    if cmd.startswith("/holdall"):
        rows = list_watches(conn, chat_id, 3001, 0)
        if not rows:
            send_md(session, token, chat_id, f"📭 监听列表为空。请先用 {md_code('/watch')} 添加地址。")
            return
        alpha_prices = get_alpha_price_map(session)
        agg = {}
        for addr, _, _ in rows:
            alpha_by_netuid = get_coldkey_alpha_summary(addr)
            for netuid, alpha_rao in alpha_by_netuid.items():
                agg[netuid] = agg.get(netuid, 0) + int(alpha_rao or 0)
        items = []
        for netuid, alpha_rao in agg.items():
            alpha = tao_from_rao(alpha_rao)
            price = alpha_prices.get(int(netuid))
            tao_equiv = (alpha * float(price)) if price is not None else None
            items.append((netuid, alpha, tao_equiv))
        items.sort(key=lambda x: (x[2] if x[2] is not None else -1.0), reverse=True)
        top = items[:30]
        lines = ["📌 *各子网持有量汇总*（当前聊天）"]
        for netuid, alpha, tao_equiv in top:
            try:
                n = int(netuid)
            except Exception:
                continue
            if tao_equiv is None:
                lines.append(f"🌠 SN{n}：{fmt_num(alpha, 3)} α（≈N/A）")
            else:
                lines.append(f"🌠 SN{n}：{fmt_num(tao_equiv, 3)} 𝞃（{fmt_num(alpha, 3)} α）")
        lines.append("")
        lines.append(f"提示：用 {md_code('/hold <子网ID>')} 看单个子网排行。")
        send_md(session, token, chat_id, "\n".join(lines))
        return

    if cmd.startswith("/hold"):
        parts = cmd.split(None, 1)
        if len(parts) < 2:
            send_md(session, token, chat_id, f"用法：{md_code('/hold <子网ID>')}")
            return
        try:
            netuid = int(parts[1].strip())
        except Exception:
            send_md(session, token, chat_id, "❌ 子网ID格式不对。")
            return
        rows = list_watches(conn, chat_id, 3001, 0)
        if not rows:
            send_md(session, token, chat_id, f"📭 监听列表为空。请先用 {md_code('/watch')} 添加地址。")
            return
        alpha_prices = get_alpha_price_map(session)
        price = alpha_prices.get(netuid)
        ranking = []
        for addr, remark, _ in rows:
            alpha_by_netuid = get_coldkey_alpha_summary(addr)
            alpha_rao = int(alpha_by_netuid.get(netuid, 0) or 0)
            alpha = tao_from_rao(alpha_rao)
            tao_equiv = (alpha * float(price)) if price is not None else None
            ranking.append((addr, remark, alpha, tao_equiv))
        ranking.sort(key=lambda x: (x[3] if x[3] is not None else x[2]), reverse=True)
        top = ranking[:30]
        lines = [f"📌 *SN{netuid} 持有量排行*（当前聊天）"]
        for idx, (addr, remark, alpha, tao_equiv) in enumerate(top, start=1):
            name = f"{short_addr(addr)}（{remark}）" if remark else short_addr(addr)
            if tao_equiv is None:
                lines.append(f"{md_code(idx)}. {md_code(name)}：{fmt_num(alpha, 3)} α（≈N/A）")
            else:
                lines.append(f"{md_code(idx)}. {md_code(name)}：{fmt_num(tao_equiv, 3)} 𝞃（{fmt_num(alpha, 3)} α）")
        send_md(session, token, chat_id, "\n".join(lines))
        return

    if cmd.startswith("/balances"):
        mode = "total"
        parts = cmd.split(None, 1)
        if len(parts) == 2:
            mode = parts[1].strip().lower()
        rows = list_watches(conn, chat_id, 3001, 0)
        if not rows:
            send_md(session, token, chat_id, f"📭 监听列表为空。请先用 {md_code('/watch')} 添加地址。")
            return
        alpha_prices = get_alpha_price_map(session)
        usd = get_tao_price_usd(session)
        sum_free = 0.0
        sum_reserved = 0.0
        sum_subnet = 0.0
        for addr, _, _ in rows:
            bal = get_system_balance_tao(addr)
            sum_free += bal["free"]
            sum_reserved += bal["reserved"]
            if mode != "f":
                alpha_by_netuid = get_coldkey_alpha_summary(addr)
                for netuid, alpha_rao in alpha_by_netuid.items():
                    price = alpha_prices.get(int(netuid))
                    if price is None:
                        continue
                    sum_subnet += tao_from_rao(alpha_rao) * float(price)
        if mode == "available":
            total = sum_free
        elif mode == "f":
            total = sum_free
        else:
            total = sum_free + sum_reserved + sum_subnet
        mode_map = {"available": "可用余额（free）", "total": "总资产（含子网）", "f": "仅free"}
        lines = [f"💰 *资产汇总*（当前聊天）", f"模式：{md_bold(mode_map.get(mode, mode))}"]
        lines.append(f"可用余额 free：{md_code(fmt_num(sum_free, 3))} 𝞃")
        lines.append(f"锁定/保留 reserved：{md_code(fmt_num(sum_reserved, 3))} 𝞃")
        if mode not in ("available", "f"):
            lines.append(f"子网合计≈{md_code(fmt_num(sum_subnet, 3))} 𝞃")
        lines.append(f"总计≈{md_bold(md_code(fmt_num(total, 3)) + ' 𝞃')}")
        if usd:
            lines.append(f"总计≈{md_code(fmt_num(total * usd, 2))} USD")
        lines.append("")
        lines.append(f"提示：{md_code('/balances available')} / {md_code('/balances total')} / {md_code('/balances f')}")
        send_md(session, token, chat_id, "\n".join(lines))
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

def format_event_zh(call_module, call_function):
    mod = str(call_module or "")
    fun = str(call_function or "")
    key = f"{mod}.{fun}"
    m = mod.lower()
    f = fun.lower()
    mapping = {
        "Balances.transfer": "转账",
        "Balances.transfer_keep_alive": "转账（保活）",
        "SubtensorModule.add_stake": "质押",
        "SubtensorModule.add_stake_limit": "质押（限价）",
        "SubtensorModule.remove_stake": "解押",
        "SubtensorModule.remove_stake_limit": "解押（限价）",
    }
    if key in mapping:
        return mapping[key]
    if m == "subtensormodule":
        if f.startswith("add_stake"):
            return "质押"
        if f.startswith("remove_stake"):
            return "解押"
    if m == "balances":
        if f.startswith("transfer"):
            return "转账"
    return key

def format_event_kind_en(call_module, call_function):
    mod = str(call_module or "")
    fun = str(call_function or "")
    key = f"{mod}.{fun}"
    mapping = {
        "Balances.transfer": "Transfer",
        "Balances.transfer_keep_alive": "TransferKeepAlive",
        "SubtensorModule.add_stake": "StakeAdded",
        "SubtensorModule.add_stake_limit": "StakeAdded",
        "SubtensorModule.remove_stake": "StakeRemoved",
        "SubtensorModule.remove_stake_limit": "StakeRemoved",
    }
    if key in mapping:
        return mapping[key]
    return fun or key

def format_side_short(ev_type):
    if ev_type == "stake":
        return "buy"
    if ev_type == "unstake":
        return "sell"
    if ev_type == "transfer":
        return "transfer"
    return "other"

def format_side_color(ev_type):
    if ev_type == "stake":
        return "🟩"
    if ev_type == "unstake":
        return "🟥"
    if ev_type == "transfer":
        return "🟦"
    return "⬜"

def get_netuid_from_args(args_dict):
    return safe_int((args_dict or {}).get("netuid") or (args_dict or {}).get("originNetuid") or (args_dict or {}).get("origin_netuid"))

def get_address_netuid_tao_equiv(address, netuid, alpha_prices):
    if netuid is None:
        return None
    try:
        alpha_by_netuid = get_coldkey_alpha_summary(address)
        alpha_rao = int(alpha_by_netuid.get(int(netuid), 0) or 0)
        alpha = tao_from_rao(alpha_rao)
        price = alpha_prices.get(int(netuid))
        if price is None:
            return None
        return alpha * float(price)
    except Exception:
        return None

def call_args_list_to_dict(call_args):
    if isinstance(call_args, dict):
        return call_args
    if not isinstance(call_args, list):
        return {}
    out = {}
    for item in call_args:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        out[name] = item.get("value")
    return out

def safe_int(v):
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None

def fmt_tao_from_rao(v):
    i = safe_int(v)
    if i is None:
        return None
    return tao_from_rao(i)

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
                call_args = call.get("call_args", [])
                args_dict = call_args_list_to_dict(call_args)
                ev_type = classify_event_type(call_module, call_function)

                watchers = find_watches_by_address(conn, signer)
                if not watchers:
                    continue
                tao_usd = get_tao_price_usd(session)
                alpha_prices = get_alpha_price_map(session)
                for (chat_id, remark, _) in watchers:
                    wanted = get_events_setting(conn, chat_id)
                    if wanted != "all" and wanted != ev_type:
                        continue
                    tx_hash = exv.get("extrinsic_hash", "")
                    netuid = get_netuid_from_args(args_dict)
                    side = format_side_short(ev_type)
                    color = format_side_color(ev_type)
                    kind = format_event_kind_en(call_module, call_function)
                    signer_short = short_addr(signer, 4, 4)
                    account_url = taostats_account_url(signer)

                    if netuid is not None:
                        lines = [f"{color}🖥️ SN{netuid}: {side} ({kind})"]
                    else:
                        lines = [f"{color}🖥️ {side} ({kind})"]

                    lines.append(f"Account: 🌟{md_code(signer_short)} ([taostats]({account_url}))")
                    if remark:
                        lines.append(f"📝 备注: {sanitize_md_code(remark)}")
                    if ev_type == "transfer":
                        dest = args_dict.get("dest") or args_dict.get("dest_addr") or args_dict.get("to")
                        tao_amt = fmt_tao_from_rao(args_dict.get("value") or args_dict.get("amount") or args_dict.get("balance"))
                        if dest:
                            lines.append(f"➡️ To: {md_code(short_addr(dest, 4, 4))}")
                        if tao_amt is not None:
                            usd_txt = f" (${fmt_num(tao_amt * tao_usd, 2)})" if tao_usd else ""
                            lines.append(f"{fmt_num(tao_amt, 6)}𝞃{usd_txt}")
                    if ev_type in ("stake", "unstake"):
                        raw_alpha = args_dict.get("amountStaked") or args_dict.get("amount_staked") or args_dict.get("amountUnstaked") or args_dict.get("amount_unstaked")
                        alpha_amt = fmt_tao_from_rao(raw_alpha)
                        if alpha_amt is not None:
                            tao_equiv = None
                            price_per_alpha = None
                            if netuid is not None and netuid in alpha_prices:
                                price_per_alpha = float(alpha_prices[netuid])
                                tao_equiv = alpha_amt * price_per_alpha

                            if tao_equiv is None:
                                lines.append(f"{fmt_num(alpha_amt, 2)}α")
                            else:
                                usd_txt = f" (${fmt_num(tao_equiv * tao_usd, 2)})" if tao_usd else ""
                                lines.append(f"{fmt_num(alpha_amt, 2)}α ⇄ {fmt_num(tao_equiv, 6)}𝞃{usd_txt}")

                            if price_per_alpha is not None:
                                price_usd_txt = f" (${fmt_num(price_per_alpha * tao_usd, 2)})" if tao_usd else ""
                                lines.append(f"Price per alpha: {fmt_num(price_per_alpha, 6)}𝞃{price_usd_txt}")

                    if netuid is not None:
                        holding_tao_equiv = get_address_netuid_tao_equiv(signer, netuid, alpha_prices)
                        if holding_tao_equiv is not None:
                            usd_txt = f" (${fmt_num(holding_tao_equiv * tao_usd, 2)})" if tao_usd else ""
                            lines.append("")
                            lines.append(f"🌠SN{netuid}:💰持有≈ {fmt_num(holding_tao_equiv, 3)} 𝞃{usd_txt}")

                    bal = get_system_balance_tao(signer)
                    lines.append(f"💰 可用余额(free): {fmt_num(bal.get('free', 0.0), 6)}𝞃")
                    if tx_hash:
                        lines.append(f"Tx: {md_code(short_addr(tx_hash, 10, 10))}")
                        lines.append(f"Hash: {md_code(tx_hash)}")
                    send_md(session, token, chat_id, "\n".join(lines))
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
