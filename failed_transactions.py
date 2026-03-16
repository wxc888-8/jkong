import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import os
import threading
import queue
from datetime import datetime, timezone, timedelta
from substrateinterface import Keypair, SubstrateInterface

# 用于记录已显示的交易哈希，防止重复打印
processed_hashes = set()

def _load_dotenv(path=".env"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:].lstrip()
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k or k in os.environ:
            continue
        if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        os.environ[k] = v

_load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID")
TELEGRAM_SEND_MODE = (os.environ.get("TELEGRAM_SEND_MODE") or os.environ.get("TG_SEND_MODE") or "batch").strip().lower()
_tg_async_raw = (os.environ.get("TELEGRAM_ASYNC") or os.environ.get("TG_ASYNC") or "1").strip().lower()
TELEGRAM_ASYNC = _tg_async_raw not in ("0", "false", "no", "off")
_admin_raw = os.environ.get("TELEGRAM_ADMIN_USER_ID") or os.environ.get("TG_ADMIN_USER_ID") or ""
TELEGRAM_ADMIN_USER_IDS = set()
for _p in str(_admin_raw).replace(";", ",").split(","):
    _p = _p.strip()
    if not _p:
        continue
    try:
        TELEGRAM_ADMIN_USER_IDS.add(int(_p))
    except Exception:
        continue
_tg_control_raw = os.environ.get("TELEGRAM_CONTROL_BOT") or os.environ.get("TG_CONTROL_BOT") or "1"
MONITOR_CONFIG_PATH = os.environ.get("MONITOR_CONFIG_PATH") or "monitor_config.json"
_tg_session = None
_tg_queue = queue.Queue(maxsize=2000)
_tg_worker_started = False
_config_lock = threading.Lock()
_config = None

def _normalize_bool(v, default):
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default

TELEGRAM_CONTROL_BOT = _normalize_bool(_tg_control_raw, True)

def _normalize_address(addr):
    if not addr:
        return None
    if isinstance(addr, str) and addr.startswith("0x"):
        return to_ss58(addr)
    return str(addr).strip()

def _parse_addresses(raw):
    out = set()
    if not raw:
        return out
    for p in str(raw).replace(";", ",").split(","):
        a = _normalize_address(p.strip())
        if a:
            out.add(a)
    return out

def _load_monitor_config():
    defaults = {
        "min_tao": float(os.environ.get("MONITOR_MIN_TAO") or 0.5),
        "mode": (os.environ.get("MONITOR_MODE") or "all").strip().lower(),
        "addresses": sorted(_parse_addresses(os.environ.get("MONITOR_ADDRESSES") or "")),
        "push_enabled": _normalize_bool(os.environ.get("TELEGRAM_PUSH_ENABLED") or "1", True),
    }
    try:
        with open(MONITOR_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("min_tao"), (int, float, str)):
                try:
                    defaults["min_tao"] = float(data.get("min_tao"))
                except Exception:
                    pass
            if isinstance(data.get("mode"), str):
                defaults["mode"] = data.get("mode").strip().lower()
            if isinstance(data.get("addresses"), list):
                defaults["addresses"] = sorted(_parse_addresses(",".join([str(x) for x in data.get("addresses") if x is not None])))
            if "push_enabled" in data:
                defaults["push_enabled"] = _normalize_bool(data.get("push_enabled"), defaults["push_enabled"])
    except Exception:
        pass
    if defaults["mode"] not in ("all", "addresses"):
        defaults["mode"] = "all"
    if defaults["min_tao"] < 0:
        defaults["min_tao"] = 0.0
    return defaults

def _save_monitor_config(cfg):
    try:
        with open(MONITOR_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_monitor_config():
    global _config
    with _config_lock:
        if _config is None:
            _config = _load_monitor_config()
        return dict(_config)

def update_monitor_config(patch):
    global _config
    with _config_lock:
        if _config is None:
            _config = _load_monitor_config()
        for k, v in patch.items():
            _config[k] = v
        if _config.get("mode") not in ("all", "addresses"):
            _config["mode"] = "all"
        try:
            _config["min_tao"] = float(_config.get("min_tao") or 0.0)
        except Exception:
            _config["min_tao"] = 0.0
        if _config["min_tao"] < 0:
            _config["min_tao"] = 0.0
        if not isinstance(_config.get("addresses"), list):
            _config["addresses"] = []
        _config["addresses"] = sorted(_parse_addresses(",".join([str(x) for x in _config.get("addresses") if x is not None])))
        _config["push_enabled"] = _normalize_bool(_config.get("push_enabled"), True)
        _save_monitor_config(_config)
        return dict(_config)

def _get_tg_session():
    global _tg_session
    if _tg_session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "tao-failed-monitor",
            "Accept": "application/json",
        })
        _tg_session = s
    return _tg_session

def _safe_truncate(s, max_len):
    if not isinstance(s, str):
        s = str(s) if s is not None else ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"

def format_failed_tx_message(ts_cn, netuid, tao_amount, alpha_amount, tao_equiv, signer, tx_hash, trade_side, fail_reason):
    netuid_text = str(netuid) if netuid is not None else "Global"
    if isinstance(tao_amount, float):
        vol_text = f"{tao_amount:,.4f} TAO"
    elif isinstance(alpha_amount, float) and isinstance(tao_equiv, float):
        vol_text = f"{tao_equiv:,.4f} TAO（{alpha_amount:,.4f} α）"
    elif isinstance(tao_equiv, float):
        vol_text = f"{tao_equiv:,.4f} TAO"
    else:
        vol_text = "未知"
    fail_reason = _safe_truncate(fail_reason, 600)
    return "\n".join([
        f"失败交易提醒",
        f"时间：{ts_cn}（北京时间）",
        f"子网：{netuid_text}",
        f"交易量：{vol_text}",
        f"钱包：{signer}",
        f"类型：{trade_side}",
        f"原因：{fail_reason}",
        f"哈希：{tx_hash}",
    ])

def format_failed_tx_line(ts_cn, netuid, tao_amount, alpha_amount, tao_equiv, signer, tx_hash, trade_side, fail_reason):
    netuid_text = str(netuid) if netuid is not None else "Global"
    if isinstance(tao_amount, float):
        vol_text = f"{tao_amount:,.4f} TAO"
    elif isinstance(alpha_amount, float) and isinstance(tao_equiv, float):
        vol_text = f"{tao_equiv:,.4f} TAO（{alpha_amount:,.4f} α）"
    elif isinstance(tao_equiv, float):
        vol_text = f"{tao_equiv:,.4f} TAO"
    else:
        vol_text = "未知"
    signer = _safe_truncate(signer, 16)
    tx_hash = _safe_truncate(tx_hash, 24)
    fail_reason = _safe_truncate(fail_reason, 140)
    return f"{ts_cn} 子网{netuid_text} {vol_text} {trade_side} {signer} {tx_hash} 原因：{fail_reason}"

def _tg_worker_loop():
    while True:
        text = _tg_queue.get()
        try:
            send_telegram_message(text)
        finally:
            _tg_queue.task_done()

def _ensure_tg_worker():
    global _tg_worker_started
    if _tg_worker_started:
        return
    if not TELEGRAM_ASYNC:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    t = threading.Thread(target=_tg_worker_loop, daemon=True)
    t.start()
    _tg_worker_started = True

def enqueue_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    if TELEGRAM_ASYNC:
        _ensure_tg_worker()
        try:
            _tg_queue.put_nowait(text)
            return True
        except Exception:
            return False
    return send_telegram_message(text)

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _safe_truncate(text, 3800),
        "disable_web_page_preview": True,
    }
    s = _get_tg_session()
    for attempt in range(3):
        try:
            resp = s.post(url, json=payload, timeout=10)
            if resp.status_code == 429:
                try:
                    data = resp.json()
                    retry_after = int((data.get("parameters") or {}).get("retry_after") or 0)
                except Exception:
                    retry_after = 0
                time.sleep(min(max(retry_after, 1), 30))
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(min(2 ** attempt, 8))
                continue
            resp.raise_for_status()
            return True
        except Exception:
            time.sleep(min(2 ** attempt, 8))
    return False

def send_telegram_message_to(chat_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": _safe_truncate(text, 3800),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    s = _get_tg_session()
    for attempt in range(3):
        try:
            resp = s.post(url, json=payload, timeout=10)
            if resp.status_code == 429:
                try:
                    data = resp.json()
                    retry_after = int((data.get("parameters") or {}).get("retry_after") or 0)
                except Exception:
                    retry_after = 0
                time.sleep(min(max(retry_after, 1), 30))
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(min(2 ** attempt, 8))
                continue
            resp.raise_for_status()
            return True
        except Exception:
            time.sleep(min(2 ** attempt, 8))
    return False

def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not chat_id or not message_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": _safe_truncate(text, 3800),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    s = _get_tg_session()
    for attempt in range(3):
        try:
            resp = s.post(url, json=payload, timeout=10)
            if resp.status_code == 429:
                try:
                    data = resp.json()
                    retry_after = int((data.get("parameters") or {}).get("retry_after") or 0)
                except Exception:
                    retry_after = 0
                time.sleep(min(max(retry_after, 1), 30))
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(min(2 ** attempt, 8))
                continue
            resp.raise_for_status()
            return True
        except Exception:
            time.sleep(min(2 ** attempt, 8))
    return False

def answer_telegram_callback(callback_query_id, text=None, show_alert=False):
    if not TELEGRAM_BOT_TOKEN or not callback_query_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {
        "callback_query_id": callback_query_id,
        "show_alert": bool(show_alert),
    }
    if isinstance(text, str) and text.strip():
        payload["text"] = _safe_truncate(text.strip(), 180)
    s = _get_tg_session()
    for attempt in range(3):
        try:
            resp = s.post(url, json=payload, timeout=10)
            if resp.status_code == 429:
                try:
                    data = resp.json()
                    retry_after = int((data.get("parameters") or {}).get("retry_after") or 0)
                except Exception:
                    retry_after = 0
                time.sleep(min(max(retry_after, 1), 30))
                continue
            if 500 <= resp.status_code < 600:
                time.sleep(min(2 ** attempt, 8))
                continue
            resp.raise_for_status()
            return True
        except Exception:
            time.sleep(min(2 ** attempt, 8))
    return False

def _admin_help_text():
    return "\n".join([
        "可用命令：",
        "/menu 打开按钮菜单",
        "/status 查看当前配置",
        "/min 0.5 设置阈值（TAO）",
        "/mode all|addresses 设置监控模式（全部/仅地址）",
        "/add <地址> 添加监控地址（可重复多次）",
        "/del <地址> 删除监控地址",
        "/list 列出监控地址",
        "/push on|off 开关推送（终端仍打印）",
        "/help 查看帮助",
    ])

def _format_status(cfg):
    mode_text = "全部" if cfg.get("mode") == "all" else "仅指定地址"
    push_text = "开" if cfg.get("push_enabled") else "关"
    addrs = cfg.get("addresses") or []
    lines = [
        f"当前配置：",
        f"阈值：{cfg.get('min_tao')} TAO",
        f"模式：{mode_text}",
        f"推送：{push_text}",
        f"地址数：{len(addrs)}",
    ]
    if addrs:
        lines.append("地址：")
        lines.extend(addrs[:50])
        if len(addrs) > 50:
            lines.append(f"... 还有 {len(addrs) - 50} 个")
    return "\n".join(lines)

def _admin_menu_keyboard(cfg):
    push_text = "推送：开" if cfg.get("push_enabled") else "推送：关"
    mode_text = "模式：全部" if cfg.get("mode") == "all" else "模式：仅地址"
    min_text = f"阈值：{cfg.get('min_tao')} TAO"
    return {
        "inline_keyboard": [
            [{"text": "刷新状态", "callback_data": "menu:status"}],
            [{"text": push_text, "callback_data": "menu:push"}, {"text": mode_text, "callback_data": "menu:mode"}],
            [{"text": "阈值 -0.5", "callback_data": "menu:min:-0.5"}, {"text": min_text, "callback_data": "menu:status"}, {"text": "阈值 +0.5", "callback_data": "menu:min:+0.5"}],
            [{"text": "阈值 -0.1", "callback_data": "menu:min:-0.1"}, {"text": "阈值 +0.1", "callback_data": "menu:min:+0.1"}],
            [{"text": "地址列表", "callback_data": "menu:list"}, {"text": "帮助", "callback_data": "menu:help"}],
        ]
    }

def _admin_menu_text(cfg):
    return _format_status(cfg) + "\n\n发送 /add <地址> /del <地址> 管理地址。"

def _handle_admin_callback(data):
    d = (data or "").strip()
    if not d.startswith("menu:"):
        return None, None
    action = d[5:]
    if action == "status":
        cfg = get_monitor_config()
        return _admin_menu_text(cfg), cfg
    if action == "help":
        return _admin_help_text(), None
    if action == "list":
        cfg = get_monitor_config()
        addrs = cfg.get("addresses") or []
        if not addrs:
            return "地址列表为空", None
        return "\n".join(["地址列表："] + addrs[:120]), None
    if action == "push":
        cfg0 = get_monitor_config()
        cfg = update_monitor_config({"push_enabled": not bool(cfg0.get("push_enabled"))})
        return _admin_menu_text(cfg), cfg
    if action == "mode":
        cfg0 = get_monitor_config()
        new_mode = "addresses" if (cfg0.get("mode") or "all") == "all" else "all"
        cfg = update_monitor_config({"mode": new_mode})
        return _admin_menu_text(cfg), cfg
    if action.startswith("min:"):
        delta_str = action[4:]
        try:
            delta = float(delta_str)
        except Exception:
            return "阈值调整失败", None
        cfg0 = get_monitor_config()
        cur = float(cfg0.get("min_tao") or 0.0)
        cfg = update_monitor_config({"min_tao": max(cur + delta, 0.0)})
        return _admin_menu_text(cfg), cfg
    return None, None

def _handle_admin_command(text):
    t = (text or "").strip()
    if not t:
        return None
    if t in ("/menu", "菜单"):
        cfg = get_monitor_config()
        return _admin_menu_text(cfg)
    if t in ("/start", "/help", "help", "帮助"):
        return _admin_help_text()
    if t.startswith("/status") or t.startswith("状态"):
        return _format_status(get_monitor_config())
    if t.startswith("/min") or t.startswith("/threshold") or t.startswith("阈值"):
        parts = t.replace("阈值", "").split()
        if len(parts) >= 2:
            val = parts[1]
        elif len(parts) == 1 and t.startswith("阈值"):
            val = parts[0].strip()
        else:
            return "用法：/min 0.5"
        try:
            f = float(val)
        except Exception:
            return "阈值格式不对，用法：/min 0.5"
        cfg = update_monitor_config({"min_tao": max(f, 0.0)})
        return f"已设置阈值为 {cfg.get('min_tao')} TAO"
    if t.startswith("/mode") or t.startswith("模式"):
        parts = t.replace("模式", "").split()
        if len(parts) >= 2:
            m = parts[1].strip().lower()
        elif len(parts) == 1 and t.startswith("模式"):
            m = parts[0].strip().lower()
        else:
            return "用法：/mode all 或 /mode addresses"
        if m in ("all", "全部"):
            m = "all"
        if m in ("addresses", "addr", "address", "only", "仅地址", "指定", "指定地址"):
            m = "addresses"
        if m not in ("all", "addresses"):
            return "模式只能是 all 或 addresses"
        cfg = update_monitor_config({"mode": m})
        return _format_status(cfg)
    if t.startswith("/add") or t.startswith("添加"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            return "用法：/add <地址>"
        addr = _normalize_address(parts[1].strip())
        if not addr:
            return "地址为空"
        cfg0 = get_monitor_config()
        addrs = set(cfg0.get("addresses") or [])
        addrs.add(addr)
        cfg = update_monitor_config({"addresses": sorted(addrs)})
        return f"已添加地址：{addr}\n地址数：{len(cfg.get('addresses') or [])}"
    if t.startswith("/del") or t.startswith("删除"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            return "用法：/del <地址>"
        addr = _normalize_address(parts[1].strip())
        if not addr:
            return "地址为空"
        cfg0 = get_monitor_config()
        addrs = set(cfg0.get("addresses") or [])
        if addr in addrs:
            addrs.remove(addr)
        cfg = update_monitor_config({"addresses": sorted(addrs)})
        return f"已删除地址：{addr}\n地址数：{len(cfg.get('addresses') or [])}"
    if t.startswith("/list") or t.startswith("列表"):
        cfg = get_monitor_config()
        addrs = cfg.get("addresses") or []
        if not addrs:
            return "地址列表为空"
        return "\n".join(["地址列表："] + addrs[:100])
    if t.startswith("/push") or t.startswith("推送"):
        parts = t.split(maxsplit=1)
        if len(parts) < 2:
            return "用法：/push on 或 /push off"
        v = parts[1].strip().lower()
        if v in ("on", "1", "true", "开"):
            cfg = update_monitor_config({"push_enabled": True})
            return _format_status(cfg)
        if v in ("off", "0", "false", "关"):
            cfg = update_monitor_config({"push_enabled": False})
            return _format_status(cfg)
        return "用法：/push on 或 /push off"
    return "未识别命令，发送 /help 查看用法"

def _run_admin_polling():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CONTROL_BOT:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    s = _get_tg_session()
    offset = 0
    while True:
        try:
            params = {
                "timeout": 30,
                "offset": offset,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
            resp = s.get(url, params=params, timeout=40)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("result") if isinstance(data, dict) else None
            if not isinstance(results, list):
                time.sleep(2)
                continue
            for upd in results:
                if not isinstance(upd, dict):
                    continue
                upd_id = upd.get("update_id")
                if isinstance(upd_id, int) and upd_id >= offset:
                    offset = upd_id + 1
                cbq = upd.get("callback_query")
                if isinstance(cbq, dict):
                    cbq_id = cbq.get("id")
                    frm = cbq.get("from") if isinstance(cbq.get("from"), dict) else {}
                    user_id = frm.get("id")
                    msg_obj = cbq.get("message") if isinstance(cbq.get("message"), dict) else {}
                    chat = msg_obj.get("chat") if isinstance(msg_obj.get("chat"), dict) else {}
                    chat_id = chat.get("id")
                    message_id = msg_obj.get("message_id")
                    data_str = cbq.get("data")
                    if user_id not in TELEGRAM_ADMIN_USER_IDS:
                        answer_telegram_callback(cbq_id, "无权限。", show_alert=False)
                        continue
                    out_text, cfg_for_kb = _handle_admin_callback(data_str)
                    if out_text:
                        if cfg_for_kb is not None:
                            kb = _admin_menu_keyboard(cfg_for_kb)
                            edit_telegram_message(chat_id, message_id, out_text, reply_markup=kb)
                        else:
                            send_telegram_message_to(chat_id, out_text)
                        answer_telegram_callback(cbq_id, "已处理", show_alert=False)
                    else:
                        answer_telegram_callback(cbq_id, "无效操作", show_alert=False)
                    continue
                msg = upd.get("message")
                if not isinstance(msg, dict):
                    continue
                frm = msg.get("from") if isinstance(msg.get("from"), dict) else {}
                user_id = frm.get("id")
                chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
                chat_id = chat.get("id")
                text = msg.get("text")
                if not isinstance(text, str):
                    continue
                t = text.strip()
                if t in ("/whoami", "/id", "id", "我的id", "我的ID"):
                    send_telegram_message_to(chat_id, f"你的 Telegram User ID：{user_id}\n当前 Chat ID：{chat_id}")
                    if not TELEGRAM_ADMIN_USER_IDS:
                        send_telegram_message_to(chat_id, "未配置管理员ID。请在 .env 添加：TELEGRAM_ADMIN_USER_ID=<你的User ID>，然后重启脚本。")
                    continue
                if TELEGRAM_ADMIN_USER_IDS and user_id not in TELEGRAM_ADMIN_USER_IDS:
                    send_telegram_message_to(chat_id, "无权限。")
                    continue
                if not TELEGRAM_ADMIN_USER_IDS:
                    send_telegram_message_to(chat_id, "未配置管理员ID。发送 /whoami 获取你的User ID，然后在 .env 配置 TELEGRAM_ADMIN_USER_ID 并重启。")
                    continue
                out = _handle_admin_command(text)
                if out:
                    if t in ("/menu", "菜单"):
                        cfg = get_monitor_config()
                        kb = _admin_menu_keyboard(cfg)
                        send_telegram_message_to(chat_id, out, reply_markup=kb)
                    else:
                        send_telegram_message_to(chat_id, out)
        except Exception:
            time.sleep(3)

def start_admin_bot():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CONTROL_BOT:
        return False
    t = threading.Thread(target=_run_admin_polling, daemon=True)
    t.start()
    return True

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
    substrate = SubstrateInterface(url="wss://entrypoint-finney.opentensor.ai:443")
    substrate.init_runtime()
    session = get_robust_session()
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
        if now - last_price_refresh >= 30 or not alpha_price_map:
            alpha_price_map = get_alpha_price_map(session)
            last_price_refresh = now

    def on_block(obj, update_nr, sub_id):
        global processed_hashes
        try:
            header = obj.get("header", {})
            block_number = header.get("number")
            if isinstance(update_nr, int) and update_nr % 5 == 0 and block_number is not None:
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
            tg_lines = []
            cfg_snapshot = get_monitor_config()
            cfg_min_tao = float(cfg_snapshot.get("min_tao") or min_tao or 0.0)
            cfg_mode = cfg_snapshot.get("mode") or "all"
            cfg_addresses = set([_normalize_address(a) for a in (cfg_snapshot.get("addresses") or []) if _normalize_address(a)])
            push_enabled = bool(cfg_snapshot.get("push_enabled"))
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
                
                if tao_equiv is None or tao_equiv < cfg_min_tao:
                    continue
                tx_hash = exv.get("extrinsic_hash", "")
                if tx_hash in processed_hashes:
                    continue
                trade_side = get_trade_side_zh(f"SubtensorModule.{call.get('call_function', '')}")
                signer = _normalize_address(exv.get("address") or "N/A") or "N/A"
                if cfg_mode == "addresses":
                    if not cfg_addresses:
                        continue
                    if signer not in cfg_addresses:
                        continue
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
                if len(processed_hashes) > 5000:
                    processed_hashes = set(list(processed_hashes)[-2000:])

                print(f"交易时间：{ts_cn}（北京时间）")
                print(f"子网编号：{netuid_int if netuid_int is not None else 'Global'}")
                if isinstance(tao_amount, float):
                    print(f"交易量：{tao_amount:,.4f} TAO")
                elif isinstance(alpha_amount, float) and isinstance(tao_equiv, float):
                    print(f"交易量：{tao_equiv:,.4f} TAO（{alpha_amount:,.4f} α）")
                elif isinstance(tao_equiv, float):
                    print(f"交易量：{tao_equiv:,.4f} TAO")
                else:
                    print("交易量：未知")
                print(f"用户钱包：{signer}")
                print(f"交易哈希：{tx_hash}")
                print(f"交易类型：{trade_side}")
                print(f"失败原因：{fail_reason}")
                print("-" * 40)
                if not push_enabled:
                    continue
                if TELEGRAM_SEND_MODE == "single":
                    msg = format_failed_tx_message(
                        ts_cn, netuid_int, tao_amount, alpha_amount, tao_equiv, signer, tx_hash, trade_side, fail_reason
                    )
                    enqueue_telegram_message(msg)
                else:
                    tg_lines.append(format_failed_tx_line(
                        ts_cn, netuid_int, tao_amount, alpha_amount, tao_equiv, signer, tx_hash, trade_side, fail_reason
                    ))
            if tg_lines and TELEGRAM_SEND_MODE != "single":
                header_line = f"失败交易提醒（共 {len(tg_lines)} 笔）"
                chunks = []
                cur = header_line
                for line in tg_lines:
                    if len(cur) + 1 + len(line) > 3600:
                        chunks.append(cur)
                        cur = header_line + "\n" + line
                    else:
                        cur = cur + "\n" + line
                if cur:
                    chunks.append(cur)
                for ch in chunks:
                    if push_enabled:
                        enqueue_telegram_message(ch)
        except Exception as e:
            print(f"处理区块 {header.get('number')} 时发生错误: {e}")
            return None
        return None

    substrate.subscribe_block_headers(on_block)

def get_failed_transactions(session, limit=100, min_tao=50.0):
    global processed_hashes
    api_url = f"https://taostats.io/api/extrinsic/extrinsic?success=false&limit={limit}"
    
    try:
        cfg = get_monitor_config()
        cfg_mode = cfg.get("mode") or "all"
        cfg_addresses = set([_normalize_address(a) for a in (cfg.get("addresses") or []) if _normalize_address(a)])
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
                    signer_addr = _normalize_address(tx.get("signer_address") or "")
                    if cfg_mode == "addresses":
                        if not cfg_addresses:
                            processed_hashes.add(tx_hash)
                            continue
                        if signer_addr not in cfg_addresses:
                            processed_hashes.add(tx_hash)
                            continue
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
        
        if len(processed_hashes) > 5000:
            processed_hashes = set(list(processed_hashes)[-2000:])

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
                if isinstance(tao_amount, float):
                    print(f"交易量：{tao_amount:,.4f} TAO")
                elif isinstance(alpha_amount, float) and isinstance(tao_equiv, float):
                    print(f"交易量：{tao_equiv:,.4f} TAO（{alpha_amount:,.4f} α）")
                elif isinstance(tao_equiv, float):
                    print(f"交易量：{tao_equiv:,.4f} TAO")
                else:
                    print("交易量：未知")
                print(f"用户钱包：{ss58_signer}")
                print(f"交易哈希：{tx_hash}")
                print(f"交易类型：{trade_side}")
                print("-" * 40)
        else:
            pass

    except Exception as e:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 抓取异常: {e}")

if __name__ == "__main__":
    cfg = get_monitor_config()
    MIN_TAO_THRESHOLD = float(cfg.get("min_tao") or 0.0)
    mode_text = "全部地址" if cfg.get("mode") == "all" else "仅指定地址"
    print(f"正在实时监控链上失败交易 (交易量 > {MIN_TAO_THRESHOLD} TAO, {mode_text})")
    if TELEGRAM_CONTROL_BOT:
        start_admin_bot()
    
    # 先检查历史失败交易，看是否有记录
    session = get_robust_session()
    print("正在获取最近历史失败交易...")
    get_failed_transactions(session, limit=10, min_tao=MIN_TAO_THRESHOLD)
    
    monitor_realtime_failed(MIN_TAO_THRESHOLD)
