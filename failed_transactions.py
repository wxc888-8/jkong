import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import os
import threading
from datetime import datetime, timezone, timedelta
from substrateinterface import Keypair, SubstrateInterface

# 用于记录已显示的交易哈希，防止重复打印
processed_hashes = set()
_tg_last_sent_ts = 0.0
_dotenv_loaded = False
_dotenv_path = None
_dotenv_cache = {}
_dotenv_mtime = None
_dotenv_lock = threading.Lock()

def load_dotenv_file(dotenv_path):
    return

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

load_dotenv_if_present()

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

ERROR_NAME_ZH = {
    "NotEnoughBalanceToStake": "余额不足，无法质押",
    "NotEnoughBalanceToUnstake": "余额不足，无法解押",
    "NotEnoughBalanceToTransfer": "余额不足，无法转账",
    "NotRegistered": "未注册",
    "AlreadyRegistered": "已注册",
    "HotKeyAccountNotExists": "热钥账户不存在",
    "ColdKeyAccountNotExists": "冷钥账户不存在",
    "NotPermitted": "权限不足",
    "RateLimitExceeded": "触发频率限制",
    "PriceLimitExceeded": "超过价格限制",
    "SlippageTooHigh": "滑点过高",
    "StakeTooSmall": "质押金额过小",
    "InvalidSignature": "签名无效",
    "Duplicate": "重复请求",
    "TooManyRegistrationsThisBlock": "本区块注册次数过多",
}

TOKEN_ERROR_ZH = {
    "FundsUnavailable": "余额不足",
    "NoFunds": "余额为零",
    "BelowMinimum": "低于最小余额要求",
    "CannotCreate": "无法创建账户",
    "UnknownAsset": "未知资产",
    "Frozen": "资金被冻结",
    "Unsupported": "不支持的操作",
}

DISPATCH_ERROR_ZH = {
    "BadOrigin": "来源权限不足",
    "CannotLookup": "无法解析地址",
    "ConsumerRemaining": "消费者引用仍在使用",
    "NoProviders": "无提供者引用",
    "TooManyConsumers": "消费者引用过多",
    "TooLowPriority": "优先级过低",
}

DOC_REPLACEMENTS_ZH = {
    "Not enough balance": "余额不足",
    "Insufficient balance": "余额不足",
    "insufficient balance": "余额不足",
    "Account balance too low": "账户余额过低",
    "Transaction would exhaust the account": "交易将耗尽账户余额",
    "The operation would exceed the price limit.": "操作将超过价格限制",
    "Slippage is too high for the transaction.": "交易滑点过高",
    "invalid": "无效",
    "Invalid": "无效",
    "Permission": "权限",
    "Not permitted": "不允许",
    "rate limit": "频率限制",
}

def zh_dispatch_error_text(pallet_name, err_name, docs_text):
    err_name = err_name or ""
    docs_text = docs_text or ""
    zh = ERROR_NAME_ZH.get(err_name)
    if not zh and docs_text:
        zh_docs = docs_text
        for k, v in DOC_REPLACEMENTS_ZH.items():
            if k in zh_docs:
                zh_docs = zh_docs.replace(k, v)
        docs_text = zh_docs
    if zh:
        if pallet_name:
            return f"[{pallet_name}] {zh}（{err_name}）"
        return f"{zh}（{err_name}）"
    if pallet_name and docs_text:
        return f"[{pallet_name}] {docs_text}（{err_name}）"
    if pallet_name:
        return f"[{pallet_name}] {err_name or '未知'}"
    if docs_text and err_name:
        return f"{docs_text}（{err_name}）"
    return err_name or docs_text or "未知"

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

def get_cn_tz():
    if ZoneInfo:
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            return timezone(timedelta(hours=8))
    return timezone(timedelta(hours=8))

CN_TZ = get_cn_tz()

def to_cn_time_str(ts):
    if not ts or not isinstance(ts, str):
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts

def short_hash(tx_hash, keep=10):
    if not tx_hash or not isinstance(tx_hash, str):
        return ""
    if len(tx_hash) <= keep:
        return tx_hash
    return tx_hash[:2] + "..." + tx_hash[-keep:]

def get_alpha_price_map(session):
    url = "https://taostats.io/api/dtao/dtaoSubnets?limit=500&order=netuid_asc"
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return {}
        rows = data.get("data", [])
        if not isinstance(rows, list):
            return {}
        out = {}
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
        return out
    except Exception:
        return {}

def get_trade_side_zh(call_name):
    """
    返回交易方向：买入/卖出/其他
    """
    if not call_name:
        return "其他"
    if call_name.startswith("SubtensorModule.add_stake"):
        return "买入"
    if call_name.startswith("SubtensorModule.remove_stake"):
        return "卖出"
    return "其他"

def get_robust_session():
    """
    创建一个带有重试机制和持久连接的 Session
    """
    session = requests.Session()
    retry_strategy = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    })
    return session

def get_telegram_config():
    token = getenv_str("TELEGRAM_BOT_TOKEN", getenv_str("TG_BOT_TOKEN", None))
    chat_id_raw = getenv_str("TELEGRAM_CHAT_ID", getenv_str("TG_CHAT_ID", None))
    if not token or not chat_id_raw:
        return None
    chat_id_s = str(chat_id_raw).strip()
    chat_ids = []
    for part in chat_id_s.replace(";", ",").replace("|", ",").split(","):
        p = part.strip()
        if not p:
            continue
        chat_ids.append(p)
    if not chat_ids:
        return None
    return (token.strip(), chat_ids)

def get_telegram_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    })
    return session

def tg_throttle(min_interval_seconds):
    global _tg_last_sent_ts
    try:
        mi = float(min_interval_seconds)
    except Exception:
        mi = 0.0
    if mi <= 0:
        _tg_last_sent_ts = time.time()
        return
    now = time.time()
    delta = now - float(_tg_last_sent_ts or 0.0)
    if delta < mi:
        time.sleep(mi - delta)
    _tg_last_sent_ts = time.time()

def tg_send_text(session, bot_token, chat_id, text):
    if not text:
        return True
    if not session or not bot_token or not chat_id:
        return False
    safe_text = str(text)
    if len(safe_text) > 3900:
        safe_text = safe_text[:3900] + "\n…(truncated)"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": safe_text,
        "disable_web_page_preview": True
    }
    try:
        resp = session.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"Telegram 推送失败: {type(e).__name__}")
        return False
    if resp.status_code != 200:
        desc = None
        try:
            data = resp.json()
            if isinstance(data, dict):
                desc = data.get("description")
        except Exception:
            desc = None
        if desc:
            print(f"Telegram 推送失败: HTTP {resp.status_code} {desc}")
        else:
            print(f"Telegram 推送失败: HTTP {resp.status_code}")
        return False
    return True

def tg_send_text_multi(session, bot_token, chat_ids, text, min_interval_seconds):
    if not chat_ids:
        return False
    ok = True
    for cid in chat_ids:
        tg_throttle(min_interval_seconds)
        if not tg_send_text(session, bot_token, cid, text):
            ok = False
    return ok

def write_dotenv_key(key, value):
    load_dotenv_if_present()
    if not _dotenv_path:
        return False
    k = (key or "").strip()
    if not k:
        return False
    v = "" if value is None else str(value)
    with _dotenv_lock:
        try:
            with open(_dotenv_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []
        out = []
        written = False
        for raw in lines:
            line = raw.rstrip("\n")
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                out.append(line)
                continue
            kk, _ = s.split("=", 1)
            if kk.strip() != k:
                out.append(line)
                continue
            if not written:
                out.append(f"{k}={v}")
                written = True
        if not written:
            if out and out[-1].strip() != "":
                out.append("")
            out.append(f"{k}={v}")
        text = "\n".join(out).rstrip() + "\n"
        try:
            with open(_dotenv_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            return False
    refresh_dotenv_cache_if_needed()
    return True

def parse_int_list(s):
    if not s:
        return []
    out = []
    for part in str(s).replace(";", ",").replace("|", ",").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.append(int(p))
        except Exception:
            continue
    return out

def get_tg_admin_user_ids():
    raw = getenv_str("TG_ADMIN_USER_IDS", getenv_str("TELEGRAM_ADMIN_USER_IDS", ""))
    return set(parse_int_list(raw))

def tg_api_get_updates(session, bot_token, offset, timeout_seconds):
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

def mask_token(t):
    s = str(t or "")
    if len(s) <= 12:
        return "****"
    return s[:6] + "..." + s[-6:]

def tg_admin_handle_message(tg_session, bot_token, message):
    if not isinstance(message, dict):
        return
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    from_user = message.get("from", {}) if isinstance(message.get("from"), dict) else {}
    user_id = from_user.get("id")
    text = message.get("text")
    if chat_id is None or user_id is None or not isinstance(text, str):
        return
    cmd = text.strip()
    if cmd == "":
        return

    is_private = (chat_type == "private")
    admins = get_tg_admin_user_ids()
    is_admin = (int(user_id) in admins) if admins else False

    if cmd.startswith("/whoami"):
        name = (from_user.get("username") or from_user.get("first_name") or "").strip()
        payload = "\n".join([
            "你的 Telegram 用户 ID：",
            str(user_id),
            f"用户名：{name}" if name else "用户名：N/A",
        ])
        tg_send_text(tg_session, bot_token, chat_id, payload)
        return

    if cmd.startswith("/help"):
        payload = "\n".join([
            "可用命令：",
            "/whoami  获取你的用户ID",
            "/show    查看当前参数",
            "/get KEY 查看某个参数",
            "/set KEY VALUE  设置参数（仅管理员，私聊）",
        ])
        tg_send_text(tg_session, bot_token, chat_id, payload)
        return

    if not is_private:
        return

    if not admins:
        payload = "\n".join([
            "未配置管理员，已拒绝管理命令。",
            "先私聊发送 /whoami 拿到用户ID，然后在 .env 里加：",
            "TG_ADMIN_USER_IDS=你的用户ID",
        ])
        tg_send_text(tg_session, bot_token, chat_id, payload)
        return

    if not is_admin:
        tg_send_text(tg_session, bot_token, chat_id, "无权限。")
        return

    if cmd.startswith("/show"):
        token = getenv_str("TELEGRAM_BOT_TOKEN", getenv_str("TG_BOT_TOKEN", ""))
        payload = "\n".join([
            f"TELEGRAM_BOT_TOKEN={mask_token(token)}",
            f"TELEGRAM_CHAT_ID={getenv_str('TELEGRAM_CHAT_ID', getenv_str('TG_CHAT_ID', ''))}",
            f"TG_MIN_INTERVAL_SECONDS={getenv_str('TG_MIN_INTERVAL_SECONDS', getenv_str('TELEGRAM_MIN_INTERVAL_SECONDS', '1'))}",
            f"MIN_TAO_THRESHOLD={getenv_str('MIN_TAO_THRESHOLD', '0.5')}",
            f"HISTORY_LIMIT={getenv_str('HISTORY_LIMIT', '10')}",
            f"SUBSTRATE_WSS_URL={getenv_str('SUBSTRATE_WSS_URL', 'wss://entrypoint-finney.opentensor.ai:443')}",
            f"PRICE_REFRESH_SECONDS={getenv_str('PRICE_REFRESH_SECONDS', '30')}",
            f"HEARTBEAT_EVERY_BLOCKS={getenv_str('HEARTBEAT_EVERY_BLOCKS', '5')}",
            f"PROCESSED_HASHES_MAX={getenv_str('PROCESSED_HASHES_MAX', '5000')}",
            f"PROCESSED_HASHES_KEEP={getenv_str('PROCESSED_HASHES_KEEP', '2000')}",
        ])
        tg_send_text(tg_session, bot_token, chat_id, payload)
        return

    if cmd.startswith("/get "):
        parts = cmd.split(None, 1)
        if len(parts) < 2:
            tg_send_text(tg_session, bot_token, chat_id, "用法：/get KEY")
            return
        key = parts[1].strip()
        if key == "":
            tg_send_text(tg_session, bot_token, chat_id, "用法：/get KEY")
            return
        val = getenv_str(key, "")
        if key in ("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN"):
            val = mask_token(val)
        tg_send_text(tg_session, bot_token, chat_id, f"{key}={val}")
        return

    if cmd.startswith("/set "):
        parts = cmd.split(None, 2)
        if len(parts) < 3:
            tg_send_text(tg_session, bot_token, chat_id, "用法：/set KEY VALUE")
            return
        key = parts[1].strip()
        value = parts[2].strip()
        blocked = {"TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN"}
        allowed = {
            "TELEGRAM_CHAT_ID",
            "TG_CHAT_ID",
            "TG_MIN_INTERVAL_SECONDS",
            "TELEGRAM_MIN_INTERVAL_SECONDS",
            "MIN_TAO_THRESHOLD",
            "HISTORY_LIMIT",
            "SUBSTRATE_WSS_URL",
            "PRICE_REFRESH_SECONDS",
            "HEARTBEAT_EVERY_BLOCKS",
            "PROCESSED_HASHES_MAX",
            "PROCESSED_HASHES_KEEP",
            "TG_ADMIN_USER_IDS",
            "TELEGRAM_ADMIN_USER_IDS",
        }
        if key in blocked:
            tg_send_text(tg_session, bot_token, chat_id, "该参数不允许通过私聊修改。")
            return
        if key not in allowed:
            tg_send_text(tg_session, bot_token, chat_id, "不支持的参数。用 /show 查看可用参数。")
            return
        file_key = "TELEGRAM_CHAT_ID" if key == "TG_CHAT_ID" else key
        file_key = "TG_MIN_INTERVAL_SECONDS" if key == "TELEGRAM_MIN_INTERVAL_SECONDS" else file_key
        ok = write_dotenv_key(file_key, value)
        if ok:
            tg_send_text(tg_session, bot_token, chat_id, f"已更新：{file_key}={value}")
        else:
            tg_send_text(tg_session, bot_token, chat_id, "更新失败（检查 .env 是否可写）。")
        return

def start_tg_admin_bot():
    token = getenv_str("TELEGRAM_BOT_TOKEN", getenv_str("TG_BOT_TOKEN", None))
    if not token:
        return None
    session = get_robust_session()
    offset = 0

    def loop():
        nonlocal offset
        while True:
            updates = tg_api_get_updates(session, token, offset, 30)
            for upd in updates:
                if not isinstance(upd, dict):
                    continue
                uid = upd.get("update_id")
                if isinstance(uid, int):
                    offset = max(offset, uid + 1)
                msg = upd.get("message")
                if isinstance(msg, dict):
                    tg_admin_handle_message(session, token, msg)
            time.sleep(1)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

def format_volume_str(tao_amount, alpha_amount, tao_equiv):
    if isinstance(tao_amount, float):
        return f"{tao_amount:,.4f} TAO"
    if isinstance(alpha_amount, float) and isinstance(tao_equiv, float):
        return f"{tao_equiv:,.4f} TAO（{alpha_amount:,.4f} α）"
    if isinstance(tao_equiv, float):
        return f"{tao_equiv:,.4f} TAO"
    return "未知"

def to_ss58(hex_address):
    """
    将 Hex 格式地址转换为 SS58 格式
    """
    if not hex_address or not hex_address.startswith('0x'):
        return hex_address
    try:
        return Keypair(public_key=hex_address, ss58_format=42).ss58_address
    except Exception:
        return hex_address

def extract_amount(tx):
    """
    从交易参数中提取交易金额 (转换为 TAO)
    """
    call_args = tx.get('call_args', {})
    amount_keys = ['amountStaked', 'amountUnstaked', 'value', 'amount', 'balance', 'alphaAmount']
    
    for key in amount_keys:
        if key in call_args:
            try:
                val = float(call_args[key])
                return val / 1_000_000_000
            except (ValueError, TypeError):
                continue
    return 0.0

def extract_trade_volume(tx, alpha_price_map):
    call_name = tx.get("full_name", "") or ""
    call_args = tx.get("call_args", {})
    if not isinstance(call_args, dict):
        call_args = {}

    netuid = call_args.get("netuid")
    if netuid is None:
        netuid = call_args.get("originNetuid")
    try:
        netuid_int = int(netuid) if netuid is not None else None
    except Exception:
        netuid_int = None

    if call_name.startswith("SubtensorModule.add_stake"):
        raw = call_args.get("amountStaked")
        if raw is None:
            return (None, None, None, netuid_int)
        try:
            tao = float(raw) / 1_000_000_000
            return (tao, None, tao, netuid_int)
        except Exception:
            return (None, None, None, netuid_int)

    if call_name.startswith("SubtensorModule.remove_stake"):
        raw = call_args.get("amountUnstaked")
        if raw is None:
            return (None, None, None, netuid_int)
        try:
            alpha = float(raw) / 1_000_000_000
        except Exception:
            return (None, None, None, netuid_int)
        price = alpha_price_map.get(netuid_int) if netuid_int is not None else None
        tao_equiv = (alpha * price) if isinstance(price, float) else None
        return (None, alpha, tao_equiv, netuid_int)

    raw_value = call_args.get("value")
    if raw_value is not None:
        try:
            tao = float(raw_value) / 1_000_000_000
            return (tao, None, tao, netuid_int)
        except Exception:
            pass

    raw_alpha = call_args.get("alphaAmount")
    if raw_alpha is not None:
        try:
            alpha = float(raw_alpha) / 1_000_000_000
        except Exception:
            return (None, None, None, netuid_int)
        price = alpha_price_map.get(netuid_int) if netuid_int is not None else None
        tao_equiv = (alpha * price) if isinstance(price, float) else None
        return (None, alpha, tao_equiv, netuid_int)

    return (None, None, None, netuid_int)

def call_args_list_to_dict(call_args):
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

def extract_trade_volume_from_call(call_name, args_dict, alpha_price_map):
    if not isinstance(args_dict, dict):
        args_dict = {}

    netuid = args_dict.get("netuid")
    if netuid is None:
        netuid = args_dict.get("originNetuid")
    if netuid is None:
        netuid = args_dict.get("origin_netuid")
        
    try:
        netuid_int = int(netuid) if netuid is not None else None
    except Exception:
        netuid_int = None

    if call_name.startswith("SubtensorModule.add_stake"):
        raw = args_dict.get("amountStaked") or args_dict.get("amount_staked")
        if raw is None:
            return (None, None, None, netuid_int)
        try:
            tao = float(raw) / 1_000_000_000
            return (tao, None, tao, netuid_int)
        except Exception:
            return (None, None, None, netuid_int)

    if call_name.startswith("SubtensorModule.remove_stake"):
        raw = args_dict.get("amountUnstaked") or args_dict.get("amount_unstaked")
        if raw is None:
            return (None, None, None, netuid_int)
        try:
            alpha = float(raw) / 1_000_000_000
        except Exception:
            return (None, None, None, netuid_int)
        price = alpha_price_map.get(netuid_int) if netuid_int is not None else None
        tao_equiv = (alpha * price) if isinstance(price, float) else None
        return (None, alpha, tao_equiv, netuid_int)

    raw_value = args_dict.get("value") or args_dict.get("amount") or args_dict.get("balance")
    if raw_value is not None:
        try:
            tao = float(raw_value) / 1_000_000_000
            return (tao, None, tao, netuid_int)
        except Exception:
            pass

    raw_alpha = args_dict.get("alphaAmount") or args_dict.get("alpha_amount")
    if raw_alpha is not None:
        try:
            alpha = float(raw_alpha) / 1_000_000_000
        except Exception:
            return (None, None, None, netuid_int)
        price = alpha_price_map.get(netuid_int) if netuid_int is not None else None
        tao_equiv = (alpha * price) if isinstance(price, float) else None
        return (None, alpha, tao_equiv, netuid_int)

    return (None, None, None, netuid_int)

def get_block_cn_time_str(extrinsics):
    if not isinstance(extrinsics, list):
        return "N/A"
    for ex in extrinsics:
        try:
            v = ex.value
        except Exception:
            continue
        call = v.get("call", {})
        if not isinstance(call, dict):
            continue
        if call.get("call_module") == "Timestamp" and call.get("call_function") == "set":
            args = call.get("call_args", [])
            args_dict = call_args_list_to_dict(args)
            now_ms = args_dict.get("now")
            try:
                ts = datetime.fromtimestamp(int(now_ms) / 1000, tz=timezone.utc).astimezone(CN_TZ)
                return ts.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return "N/A"
    return "N/A"

def monitor_realtime_failed(min_tao):
    global processed_hashes
    wss_url = getenv_str("SUBSTRATE_WSS_URL", "wss://entrypoint-finney.opentensor.ai:443")
    substrate = SubstrateInterface(url=wss_url)
    substrate.init_runtime()
    session = get_robust_session()
    tg = get_telegram_config()
    tg_session = get_telegram_session() if tg else None
    alpha_price_map = {}
    last_price_refresh = 0.0

    def decode_dispatch_error(dispatch_error):
        if not dispatch_error:
            return "未知"
        if isinstance(dispatch_error, dict):
            if "Token" in dispatch_error:
                token_err = dispatch_error.get("Token")
                if isinstance(token_err, str):
                    zh = TOKEN_ERROR_ZH.get(token_err, token_err)
                    return f"[Token] {zh}（{token_err}）"
            if "Arithmetic" in dispatch_error:
                ar_err = dispatch_error.get("Arithmetic")
                if isinstance(ar_err, str):
                    return f"[Arithmetic] 算术错误（{ar_err}）"
            if "Transactional" in dispatch_error:
                tx_err = dispatch_error.get("Transactional")
                if isinstance(tx_err, str):
                    return f"[Transactional] 事务错误（{tx_err}）"
            for k, v in DISPATCH_ERROR_ZH.items():
                if dispatch_error.get(k) is None and k in dispatch_error:
                    return v
        if isinstance(dispatch_error, dict) and "Module" in dispatch_error and isinstance(dispatch_error.get("Module"), dict):
            module_index = dispatch_error["Module"].get("index")
            error_hex = dispatch_error["Module"].get("error")
            error_index = None
            if isinstance(error_hex, str) and error_hex.startswith("0x"):
                try:
                    error_index = int.from_bytes(bytes.fromhex(error_hex[2:]), byteorder="little")
                except Exception:
                    error_index = None
            pallet_name = None
            try:
                pallet = substrate.metadata.get_pallet_by_index(module_index)
                if hasattr(pallet, "get"):
                    pallet_name = pallet.get("name")
                else:
                    pallet_name = getattr(pallet, "name", None)
            except Exception:
                pallet_name = None
            try:
                variant = substrate.metadata.get_module_error(module_index, error_index)
                err_name = getattr(variant, "name", None) or (variant.get("name") if hasattr(variant, "get") else None) or "未知"
                docs = getattr(variant, "docs", None) or (variant.get("docs") if hasattr(variant, "get") else None) or []
                if isinstance(docs, list):
                    docs_text = " ".join([d for d in docs if isinstance(d, str)]).strip()
                else:
                    docs_text = str(docs).strip() if docs else ""
                return zh_dispatch_error_text(pallet_name, err_name, docs_text)
            except Exception:
                if pallet_name and error_index is not None:
                    return f"[{pallet_name}] 错误索引 {error_index}"
                return str(dispatch_error)
        return str(dispatch_error)

    def refresh_prices_if_needed():
        nonlocal alpha_price_map, last_price_refresh
        now = time.time()
        price_refresh_seconds = getenv_float("PRICE_REFRESH_SECONDS", 30.0)
        if now - last_price_refresh >= price_refresh_seconds or not alpha_price_map:
            alpha_price_map = get_alpha_price_map(session)
            last_price_refresh = now

    def on_block(obj, update_nr, sub_id):
        global processed_hashes
        try:
            header = obj.get("header", {})
            block_number = header.get("number")
            heartbeat_every_blocks = getenv_int("HEARTBEAT_EVERY_BLOCKS", 5)
            if isinstance(update_nr, int) and heartbeat_every_blocks > 0 and update_nr % heartbeat_every_blocks == 0 and block_number is not None:
                print(f"心跳：正在监控新区块 {block_number}...")
            block_hash = substrate.get_block_hash(block_number)
            block = substrate.get_block(block_hash=block_hash)
            extrinsics = block.get("extrinsics", [])
            events = substrate.get_events(block_hash=block_hash)
            failed_idx = set()
            failed_reason_by_idx = {}
            for ev in events:
                try:
                    v = ev.value
                except Exception:
                    continue
                if v.get("module_id") == "System" and v.get("event_id") == "ExtrinsicFailed":
                    idx = v.get("extrinsic_idx")
                    if isinstance(idx, int):
                        failed_idx.add(idx)
                        attrs = v.get("attributes", {}) if isinstance(v.get("attributes", {}), dict) else {}
                        dispatch_error = attrs.get("dispatch_error")
                        if idx not in failed_reason_by_idx:
                            failed_reason_by_idx[idx] = decode_dispatch_error(dispatch_error)

            if not failed_idx:
                return None

            refresh_prices_if_needed()
            ts_cn = get_block_cn_time_str(extrinsics)

            batch_seen = set()
            for idx in sorted(failed_idx):
                if idx < 0 or idx >= len(extrinsics):
                    continue
                ex = extrinsics[idx]
                try:
                    exv = ex.value
                except Exception:
                    continue
                call = exv.get("call", {})
                if not isinstance(call, dict):
                    continue
                full_name = f"{call.get('call_module')}.{call.get('call_function')}"
                call_args = call.get("call_args", [])
                
                # 诊断：打印原始参数，看看到底是什么格式
                # print(f"  [诊断] {full_name} 参数格式: {type(call_args)}, 内容: {call_args}")
                
                args_dict = call_args_list_to_dict(call_args)
                if not args_dict and isinstance(call_args, dict):
                    args_dict = call_args
                
                tao_amount, alpha_amount, tao_equiv, netuid_int = extract_trade_volume_from_call(
                    full_name, args_dict, alpha_price_map
                )
                
                current_min_tao = getenv_float("MIN_TAO_THRESHOLD", float(min_tao) if isinstance(min_tao, (int, float)) else 0.5)
                if tao_equiv is None or tao_equiv < current_min_tao:
                    continue
                tx_hash = exv.get("extrinsic_hash", "")
                if tx_hash in processed_hashes:
                    continue
                trade_side = get_trade_side_zh(f"SubtensorModule.{call.get('call_function', '')}")
                signer = exv.get("address") or "N/A"
                fail_reason = failed_reason_by_idx.get(idx, "未知")
                fingerprint = (
                    signer,
                    full_name,
                    netuid_int,
                    args_dict.get("hotkey"),
                    args_dict.get("limitPrice"),
                    round(tao_equiv, 4),
                )
                if fingerprint in batch_seen:
                    continue
                batch_seen.add(fingerprint)
                processed_hashes.add(tx_hash)
                processed_hashes_max = getenv_int("PROCESSED_HASHES_MAX", 5000)
                processed_hashes_keep = getenv_int("PROCESSED_HASHES_KEEP", 2000)
                if len(processed_hashes) > processed_hashes_max:
                    processed_hashes = set(list(processed_hashes)[-processed_hashes_keep:])

                print(f"交易时间：{ts_cn}（北京时间）")
                print(f"子网编号：{netuid_int if netuid_int is not None else 'Global'}")
                vol_str = format_volume_str(tao_amount, alpha_amount, tao_equiv)
                if vol_str == "未知":
                    print("交易量：未知")
                else:
                    print(f"交易量：{vol_str}")
                print(f"用户钱包：{signer}")
                print(f"交易哈希：{tx_hash}")
                print(f"交易类型：{trade_side}")
                print(f"失败原因：{fail_reason}")
                print("-" * 40)
                tg2 = get_telegram_config()
                if tg2:
                    token, chat_ids = tg2
                    tg_min_interval = getenv_str("TG_MIN_INTERVAL_SECONDS", getenv_str("TELEGRAM_MIN_INTERVAL_SECONDS", "1"))
                    msg = "\n".join([
                        "链上失败交易",
                        f"时间：{ts_cn}（北京时间）",
                        f"子网：{netuid_int if netuid_int is not None else 'Global'}",
                        f"交易量：{vol_str}",
                        f"钱包：{signer}",
                        f"哈希：{tx_hash}",
                        f"类型：{trade_side}",
                        f"原因：{fail_reason}",
                    ])
                    tg_send_text_multi(tg_session or get_telegram_session(), token, chat_ids, msg, tg_min_interval)
        except Exception as e:
            print(f"处理区块 {header.get('number')} 时发生错误: {e}")
            return None
        return None

    substrate.subscribe_block_headers(on_block)

def get_failed_transactions(session, limit=100, min_tao=50.0):
    global processed_hashes
    api_url = f"https://taostats.io/api/extrinsic/extrinsic?success=false&limit={limit}"
    tg = get_telegram_config()
    tg_session = get_telegram_session() if tg else None
    tg_min_interval = getenv_str("TG_MIN_INTERVAL_SECONDS", getenv_str("TELEGRAM_MIN_INTERVAL_SECONDS", "1"))
    
    try:
        alpha_price_map = get_alpha_price_map(session)
        response = session.get(api_url, timeout=15)
        response.raise_for_status()
        result = response.json()
        
        if not isinstance(result, dict): return

        transactions = result.get('data', [])
        if not isinstance(transactions, list): transactions = []
        
        new_transactions = []
        batch_seen = set()
        for tx in transactions:
            if not isinstance(tx, dict): continue
            tx_hash = tx.get('hash')
            
            if tx_hash and tx_hash not in processed_hashes:
                tao_amount, alpha_amount, tao_equiv, netuid_int = extract_trade_volume(tx, alpha_price_map)
                if tao_equiv is None:
                    processed_hashes.add(tx_hash)
                    continue
                if tao_equiv >= min_tao:
                    tx['trade_tao'] = tao_amount
                    tx['trade_alpha'] = alpha_amount
                    tx['trade_tao_equiv'] = tao_equiv
                    tx['trade_netuid'] = netuid_int
                    call_args = tx.get('call_args', {})
                    if not isinstance(call_args, dict):
                        call_args = {}
                    fingerprint = (
                        tx.get('signer_address'),
                        tx.get('full_name'),
                        netuid_int,
                        call_args.get('hotkey'),
                        call_args.get('limitPrice'),
                        round(tao_equiv, 4),
                    )
                    if fingerprint not in batch_seen:
                        batch_seen.add(fingerprint)
                        new_transactions.append(tx)
                processed_hashes.add(tx_hash)
        
        processed_hashes_max = getenv_int("PROCESSED_HASHES_MAX", 5000)
        processed_hashes_keep = getenv_int("PROCESSED_HASHES_KEEP", 2000)
        if len(processed_hashes) > processed_hashes_max:
            processed_hashes = set(list(processed_hashes)[-processed_hashes_keep:])

        if new_transactions:
            new_transactions.sort(key=lambda x: x.get('timestamp', ''))
            
            for tx in new_transactions:
                ts = to_cn_time_str(tx.get('timestamp', 'N/A'))
                ss58_signer = to_ss58(tx.get('signer_address', 'N/A'))
                call_name = tx.get('full_name', '')
                tao_amount = tx.get('trade_tao')
                alpha_amount = tx.get('trade_alpha')
                tao_equiv = tx.get('trade_tao_equiv')
                netuid = tx.get('trade_netuid', 'Global')
                trade_side = get_trade_side_zh(call_name)
                tx_hash = tx.get('hash', '')

                print(f"交易时间：{ts}（北京时间）")
                print(f"子网编号：{netuid}")
                vol_str = format_volume_str(tao_amount, alpha_amount, tao_equiv)
                if vol_str == "未知":
                    print("交易量：未知")
                else:
                    print(f"交易量：{vol_str}")
                print(f"用户钱包：{ss58_signer}")
                print(f"交易哈希：{tx_hash}")
                print(f"交易类型：{trade_side}")
                print("-" * 40)
                if tg:
                    token, chat_ids = tg
                    msg = "\n".join([
                        "历史失败交易",
                        f"时间：{ts}（北京时间）",
                        f"子网：{netuid}",
                        f"交易量：{vol_str}",
                        f"钱包：{ss58_signer}",
                        f"哈希：{tx_hash}",
                        f"类型：{trade_side}",
                    ])
                    tg_send_text_multi(tg_session, token, chat_ids, msg, tg_min_interval)
        else:
            pass

    except Exception as e:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 抓取异常: {e}")

if __name__ == "__main__":
    start_tg_admin_bot()
    MIN_TAO_THRESHOLD = getenv_float("MIN_TAO_THRESHOLD", 0.5)
    HISTORY_LIMIT = getenv_int("HISTORY_LIMIT", 10)
    print(f"正在实时监控链上失败交易 (交易量 > {MIN_TAO_THRESHOLD} TAO)")
    
    # 先检查历史失败交易，看是否有记录
    session = get_robust_session()
    print("正在获取最近历史失败交易...")
    get_failed_transactions(session, limit=HISTORY_LIMIT, min_tao=MIN_TAO_THRESHOLD)
    
    monitor_realtime_failed(MIN_TAO_THRESHOLD)
