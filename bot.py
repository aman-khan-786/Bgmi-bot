import os
import re
import json
import time
import html
import hashlib
import logging
import urllib.request
import threading
import asyncio
import signal
from pathlib import Path
from dataclasses import dataclass, field, asdict
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Set, List, Dict, Any, Tuple, Deque
from enum import Enum, auto

# =====================================================
# 0. ASYNCIO FIX FOR PYTHON 3.14+ (RENDER CRITICAL)
# =====================================================
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait,
    PeerIdInvalid,
    ChannelPrivate,
    ChatAdminRequired,
    UserBannedInChannel,
    MessageIdInvalid,
    RPCError,
)
from flask import Flask, jsonify

# Optional Redis — graceful fallback if missing / down
try:
    import redis.asyncio as aioredis
    HAS_REDIS_LIB = True
except ImportError:
    aioredis = None
    HAS_REDIS_LIB = False

# =====================================================
# 1. LOGGING
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("C2.CORE")

# =====================================================
# 2. CONFIG
# =====================================================
class CFG:
    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
    SESSION_STRING = os.environ["SESSION_STRING"]
    MY_CHANNEL_ID = int(os.environ["MY_CHANNEL_ID"])
    SECRET_KEY = os.environ.get("SECRET_KEY", "ALPHA_BOT")
    PORT = int(os.environ.get("PORT", 8080))
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")

    STATE_FILE = Path(os.environ.get("STATE_FILE", "c2_state.json"))
    WORKERS = int(os.environ.get("WORKERS", "4"))
    QUEUE_MAX = int(os.environ.get("QUEUE_MAX", "500"))
    DEDUP_MAX = int(os.environ.get("DEDUP_MAX", "8000"))
    KEY_DEDUP_MAX = int(os.environ.get("KEY_DEDUP_MAX", "4000"))
    MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5"))
    BASE_BACKOFF = float(os.environ.get("BASE_BACKOFF", "1.5"))
    PING_INTERVAL = int(os.environ.get("PING_INTERVAL", "240"))
    ALBUM_WAIT = float(os.environ.get("ALBUM_WAIT", "1.25"))
    RATE_LIMIT = float(os.environ.get("RATE_LIMIT", "0.35"))
    SLEEP_THRESHOLD = int(os.environ.get("SLEEP_THRESHOLD", "60"))

    # --- NEW: Redis (optional) ---
    REDIS_URL = os.environ.get("REDIS_URL", "").strip()  # e.g. redis://default:pass@host:6379/0
    REDIS_PREFIX = os.environ.get("REDIS_PREFIX", "c2x")
    MSG_TTL = int(os.environ.get("MSG_TTL", str(7 * 24 * 3600)))   # 7d
    KEY_TTL = int(os.environ.get("KEY_TTL", str(30 * 24 * 3600)))  # 30d

    # --- NEW: Webhook alerts (Discord-compatible + generic JSON) ---
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
    WEBHOOK_USERNAME = os.environ.get("WEBHOOK_USERNAME", "C2-EXTREME")
    ALERT_ON_APK = os.environ.get("ALERT_ON_APK", "1") == "1"
    ALERT_ON_KEY = os.environ.get("ALERT_ON_KEY", "1") == "1"
    ALERT_ON_ERROR = os.environ.get("ALERT_ON_ERROR", "1") == "1"
    ALERT_ON_BOOT = os.environ.get("ALERT_ON_BOOT", "1") == "1"


# =====================================================
# 3. KEY INTEL
# =====================================================
KEY_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("generic_token", re.compile(r"\b[A-Za-z0-9_-]{20,64}\b")),
    ("hex_key", re.compile(r"\b[A-Fa-f0-9]{32,64}\b")),
    ("uuid_like", re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    )),
    ("license_block", re.compile(r"\b(?:[A-Z0-9]{4,5}-){3,6}[A-Z0-9]{4,5}\b")),
    ("base64ish", re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b")),
]

NOISE_RE = re.compile(
    r"^(https?://|www.|t.me/|@|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$)",
    re.I,
)

APK_MIME = "android.package-archive"
APK_EXTS = (".apk", ".xapk", ".apks", ".apkm")


def extract_keys(text: str) -> List[Tuple[str, str]]:
    if not text:
        return []
    found: Dict[str, str] = {}
    priority = {n: i for i, (n, _) in enumerate(KEY_PATTERNS)}
    for name, pat in KEY_PATTERNS:
        for m in pat.finditer(text):
            k = m.group(0).strip()
            if len(k) < 16 or NOISE_RE.search(k):
                continue
            if k not in found or priority[name] < priority[found[k]]:
                found[k] = name
    items = [(p, k) for k, p in found.items()]
    items.sort(key=lambda x: (priority.get(x[0], 99), -len(x[1])))
    return items


def is_apk(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    return name.endswith(APK_EXTS) or APK_MIME in mime


def msg_fingerprint(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


def key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:24]


# =====================================================
# 4. PER-CHANNEL FILTER RULES
# =====================================================
# Modes: all | apk | keys | media | text | off
VALID_MODES = frozenset({"all", "apk", "keys", "media", "text", "off"})


@dataclass
class ChannelRule:
    mode: str = "all"          # what to intercept
    dest_override: List[int] = field(default_factory=list)  # empty = use global dests
    min_key_len: int = 16
    keywords_any: List[str] = field(default_factory=list)   # if set, text must contain ≥1
    keywords_deny: List[str] = field(default_factory=list)  # drop if any match
    silent: bool = False       # no webhook for this channel

    def allows_apk(self) -> bool:
        return self.mode in ("all", "apk", "media")

    def allows_keys(self) -> bool:
        return self.mode in ("all", "keys", "text")

    def allows_copy_generic(self) -> bool:
        return self.mode in ("all", "media")

    def text_ok(self, text: str) -> bool:
        t = (text or "").lower()
        for d in self.keywords_deny:
            if d.lower() in t:
                return False
        if self.keywords_any:
            return any(k.lower() in t for k in self.keywords_any)
        return True


def rule_from_dict(d: dict) -> ChannelRule:
    mode = str(d.get("mode", "all")).lower()
    if mode not in VALID_MODES:
        mode = "all"
    return ChannelRule(
        mode=mode,
        dest_override=[int(x) for x in d.get("dest_override", [])],
        min_key_len=int(d.get("min_key_len", 16)),
        keywords_any=list(d.get("keywords_any", [])),
        keywords_deny=list(d.get("keywords_deny", [])),
        silent=bool(d.get("silent", False)),
    )


# =====================================================
# 5. PERSISTENT STATE
# =====================================================
@dataclass
class EngineState:
    active_channels: List[int] = field(default_factory=list)
    destinations: List[int] = field(default_factory=list)
    # channel_id(str) -> rule dict
    channel_rules: Dict[str, dict] = field(default_factory=dict)
    paused: bool = False
    total_intercepts: int = 0
    total_apks: int = 0
    total_keys: int = 0
    total_errors: int = 0
    total_alerts: int = 0
    boot_count: int = 0
    last_hit_iso: str = ""

    def save(self, path: Path = CFG.STATE_FILE) -> None:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            log.error("State save failed: %s", e)

    @classmethod
    def load(cls, path: Path = CFG.STATE_FILE) -> "EngineState":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                st = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
                log.info("State restored | ch=%s dests=%s rules=%s",
                         st.active_channels, st.destinations, len(st.channel_rules))
                return st
            except Exception as e:
                log.warning("State corrupt, fresh boot: %s", e)
        return cls(destinations=[CFG.MY_CHANNEL_ID])


state = EngineState.load()
state.boot_count += 1
if CFG.MY_CHANNEL_ID not in state.destinations:
    state.destinations.insert(0, CFG.MY_CHANNEL_ID)
state.save()

active_channels: Set[int] = set(state.active_channels)
destinations: List[int] = list(state.destinations)
channel_rules: Dict[int, ChannelRule] = {
    int(k): rule_from_dict(v) for k, v in state.channel_rules.items()
}
engine_paused = state.paused

# Memory rings (always on — Redis is L2)
processed_msgs: Deque[str] = deque(maxlen=CFG.DEDUP_MAX)
processed_set: Set[str] = set()
seen_keys: Deque[str] = deque(maxlen=CFG.KEY_DEDUP_MAX)
seen_keys_set: Set[str] = set()
_lock = threading.Lock()


def _ring_add(dq: Deque[str], s: Set[str], item: str) -> bool:
    if item in s:
        return False
    if len(dq) == dq.maxlen and dq:
        s.discard(dq[0])
    dq.append(item)
    s.add(item)
    return True


def get_rule(chat_id: int) -> ChannelRule:
    return channel_rules.get(chat_id) or ChannelRule()


def dests_for(chat_id: int) -> List[int]:
    r = get_rule(chat_id)
    return list(r.dest_override) if r.dest_override else list(destinations)


def persist():
    state.active_channels = sorted(active_channels)
    state.destinations = list(destinations)
    state.channel_rules = {str(k): asdict(v) for k, v in channel_rules.items()}
    state.paused = engine_paused
    with _lock:
        state.save()


# =====================================================
# 6. REDIS DEDUP LAYER (L2) + MEMORY (L1)
# =====================================================
redis_client = None  # type: Optional[Any]
redis_ok = False


async def redis_connect():
    global redis_client, redis_ok
    if not CFG.REDIS_URL or not HAS_REDIS_LIB:
        log.info("Redis off → memory-only dedup (lib=%s url=%s)",
                 HAS_REDIS_LIB, bool(CFG.REDIS_URL))
        redis_ok = False
        return
    try:
        redis_client = aioredis.from_url(
            CFG.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        await redis_client.ping()
        redis_ok = True
        log.info("Redis ONLINE | prefix=%s", CFG.REDIS_PREFIX)
    except Exception as e:
        log.warning("Redis connect fail → memory fallback: %s", e)
        redis_client = None
        redis_ok = False


async def redis_claim(kind: str, fp: str, ttl: int) -> bool:
    """
    SET key 1 NX EX ttl
    True  = first time (claim success) → process
    False = already seen
    On Redis error → fall through to caller memory check only
    """
    global redis_ok
    if not redis_ok or redis_client is None:
        return True  # let memory decide
    key = f"{CFG.REDIS_PREFIX}:{kind}:{fp}"
    try:
        # nx=True → only set if not exists; returns True/None
        res = await redis_client.set(key, "1", ex=ttl, nx=True)
        return bool(res)
    except Exception as e:
        log.warning("Redis claim error (degrade): %s", e)
        redis_ok = False
        return True


async def mark_msg_async(chat_id: int, msg_id: int) -> bool:
    fp = msg_fingerprint(chat_id, msg_id)
    # L1 memory first (fast reject)
    if fp in processed_set:
        return False
    # L2 Redis claim
    claimed = await redis_claim("msg", fp, CFG.MSG_TTL)
    if not claimed:
        _ring_add(processed_msgs, processed_set, fp)  # sync L1
        return False
    return _ring_add(processed_msgs, processed_set, fp)


async def mark_key_async(key: str) -> bool:
    fp = key_fingerprint(key)
    if fp in seen_keys_set:
        return False
    claimed = await redis_claim("key", fp, CFG.KEY_TTL)
    if not claimed:
        _ring_add(seen_keys, seen_keys_set, fp)
        return False
    return _ring_add(seen_keys, seen_keys_set, fp)


async def redis_flush_prefix(kind: str) -> int:
    """SCAN + DEL for CLEARDEDUP when Redis on. Returns deleted count."""
    if not redis_ok or redis_client is None:
        return 0
    n = 0
    pattern = f"{CFG.REDIS_PREFIX}:{kind}:*"
    try:
        async for key in redis_client.scan_iter(match=pattern, count=200):
            await redis_client.delete(key)
            n += 1
    except Exception as e:
        log.warning("Redis flush error: %s", e)
    return n


# =====================================================
# 7. WEBHOOK ALERT ENGINE
# =====================================================
http_session = None  # aiohttp optional; urllib fallback


def _webhook_enabled() -> bool:
    return bool(CFG.WEBHOOK_URL)


async def send_webhook(
    title: str,
    description: str,
    color: int = 0xE74C3C,
    fields: Optional[List[Dict[str, Any]]] = None,
    event: str = "generic",
):
    if not _webhook_enabled():
        return
    fields = fields or []
    # Discord-compatible embed payload (also works as generic JSON POST)
    payload = {
        "username": CFG.WEBHOOK_USERNAME,
        "content": None,
        "embeds": [{
            "title": title[:256],
            "description": description[:4000],
            "color": color,
            "fields": [
                {"name": f["name"][:256], "value": str(f["value"])[:1024], "inline": f.get("inline", True)}
                for f in fields
            ],
            "footer": {"text": f"C2-EXTREME · {event}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        # generic consumers can use these top-level keys
        "event": event,
        "engine": "C2-EXTREME",
    }

    try:
        # Prefer aiohttp if present
        try:
            import aiohttp
            global http_session
            if http_session is None or http_session.closed:
                http_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10)
                )
            async with http_session.post(CFG.WEBHOOK_URL, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning("Webhook HTTP %s: %s", resp.status, body[:200])
                else:
                    state.total_alerts += 1
        except ImportError:
            # Sync fallback in thread so we don't block loop hard
            def _post():
                req = urllib.request.Request(
                    CFG.WEBHOOK_URL,
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            status = await asyncio.to_thread(_post)
            if status and int(status) < 400:
                state.total_alerts += 1
    except Exception as e:
        log.warning("Webhook fail: %s", e)


async def alert_apk(chat_id: int, msg_id: int, fname: str, dest: int, silent: bool):
    if silent or not CFG.ALERT_ON_APK:
        return
    await send_webhook(
        title="📦 APK Captured",
        description=f"`{html.escape(fname)}`",
        color=0x2ECC71,
        fields=[
            {"name": "Source", "value": str(chat_id)},
            {"name": "Msg", "value": str(msg_id)},
            {"name": "Dest", "value": str(dest)},
        ],
        event="apk",
    )


async def alert_key(chat_id: int, msg_id: int, key: str, pattern: str, dest: int, silent: bool):
    if silent or not CFG.ALERT_ON_KEY:
        return
    masked = key if len(key) <= 12 else f"{key[:6]}…{key[-4:]}"
    await send_webhook(
        title="🔑 Key Detected",
        description=f"`{html.escape(key)}`",
        color=0xF1C40F,
        fields=[
            {"name": "Pattern", "value": pattern},
            {"name": "Preview", "value": masked},
            {"name": "Source", "value": str(chat_id)},
            {"name": "Dest", "value": str(dest)},
        ],
        event="key",
    )


async def alert_error(where: str, err: str):
    if not CFG.ALERT_ON_ERROR:
        return
    await send_webhook(
        title="⚠️ Engine Error",
        description=f"**{html.escape(where)}**
```{html.escape(err[:500])}```",
        color=0xE74C3C,
        event="error",
    )


# =====================================================
# 8. WEB + SELF-PING
# =====================================================
web_app = Flask(__name__)
_boot_ts = time.time()
_metrics = {
    "queue_depth": 0,
    "workers_alive": 0,
    "last_ping_ok": None,
    "flood_waits": 0,
    "redis": False,
    "webhook": bool(CFG.WEBHOOK_URL),
}


@web_app.route("/")
def health_check():
    return jsonify({
        "status": "ONLINE" if not engine_paused else "PAUSED",
        "engine": "C2-EXTREME v4",
        "uptime_sec": int(time.time() - _boot_ts),
        "targets": len(active_channels),
        "destinations": len(destinations),
        "rules": {str(k): asdict(v) for k, v in channel_rules.items()},
        "stats": {
            "intercepts": state.total_intercepts,
            "apks": state.total_apks,
            "keys": state.total_keys,
            "errors": state.total_errors,
            "alerts": state.total_alerts,
            "boots": state.boot_count,
            "last_hit": state.last_hit_iso,
        },
        "runtime": {**_metrics, "redis": redis_ok},
    })


@web_app.route("/health")
def health_probe():
    return "OK", 200


def run_web():
    web_app.run(host="0.0.0.0", port=CFG.PORT, threaded=True, use_reloader=False)


def auto_wake_engine():
    while True:
        time.sleep(CFG.PING_INTERVAL)
        try:
            urllib.request.urlopen(CFG.RENDER_URL, timeout=15)
            _metrics["last_ping_ok"] = datetime.now(timezone.utc).isoformat()
            log.info("Self-Ping OK")
        except Exception as e:
            log.warning("Self-Ping miss: %s", e)


threading.Thread(target=run_web, daemon=True, name="web").start()
threading.Thread(target=auto_wake_engine, daemon=True, name="ping").start()

# =====================================================
# 9. JOB QUEUE + FLOOD RETRY
# =====================================================
class JobKind(Enum):
    COPY = auto()
    KEY = auto()


@dataclass
class Job:
    kind: JobKind
    chat_id: int
    message_id: int
    dest_id: int
    payload: Dict[str, Any] = field(default_factory=dict)
    attempt: int = 0


job_queue: Optional[asyncio.Queue] = None
rate_lock: Optional[asyncio.Lock] = None
last_send_ts = 0.0


async def rate_gate():
    global last_send_ts
    async with rate_lock:
        now = time.monotonic()
        wait = CFG.RATE_LIMIT - (now - last_send_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        last_send_ts = time.monotonic()


async def with_flood_retry(coro_factory, label: str = "op"):
    delay = CFG.BASE_BACKOFF
    last_err = None
    for attempt in range(1, CFG.MAX_RETRIES + 1):
        try:
            await rate_gate()
            return await coro_factory()
        except FloodWait as e:
            _metrics["flood_waits"] += 1
            wait = int(getattr(e, "value", None) or getattr(e, "x", 5)) + 1
            log.warning("FloodWait %ss on %s (try %s)", wait, label, attempt)
            await asyncio.sleep(wait)
            delay = CFG.BASE_BACKOFF
        except (PeerIdInvalid, ChannelPrivate, UserBannedInChannel, ChatAdminRequired) as e:
            log.error("Fatal peer on %s: %s", label, e)
            raise
        except MessageIdInvalid as e:
            log.warning("Msg gone on %s: %s", label, e)
            raise
        except RPCError as e:
            last_err = e
            await asyncio.sleep(delay + 0.1 * attempt)
            delay = min(delay * 2, 60)
        except Exception as e:
            last_err = e
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError(f"{label} failed after {CFG.MAX_RETRIES}: {last_err}")


# =====================================================
# 10. ALBUM AGGREGATOR
# =====================================================
@dataclass
class AlbumBucket:
    messages: List[Message] = field(default_factory=list)
    task: Optional[asyncio.Task] = None


albums: Dict[Tuple[int, str], AlbumBucket] = {}
album_lock: Optional[asyncio.Lock] = None


async def enqueue(job: Job):
    if job_queue is None:
        return
    try:
        job_queue.put_nowait(job)
        _metrics["queue_depth"] = job_queue.qsize()
    except asyncio.QueueFull:
        log.error("Queue full — drop %s", job.kind)
        state.total_errors += 1


async def flush_album(client: Client, chat_id: int, group_id: str):
    await asyncio.sleep(CFG.ALBUM_WAIT)
    async with album_lock:
        bucket = albums.pop((chat_id, group_id), None)
    if not bucket or not bucket.messages:
        return

    rule = get_rule(chat_id)
    if rule.mode == "off" or engine_paused:
        return

    msgs = sorted(bucket.messages, key=lambda m: m.id)
    fresh = []
    for m in msgs:
        if await mark_msg_async(chat_id, m.id):
            fresh.append(m)
    if not fresh:
        return

    has_apk = any(is_apk(m) for m in fresh)
    text_blob = " ".join((m.caption or m.text or "") for m in fresh)
    if not rule.text_ok(text_blob):
        return

    keys = extract_keys(text_blob)
    keys = [(p, k) for p, k in keys if len(k) >= rule.min_key_len]
    dests = dests_for(chat_id)

    for dest in dests:
        if has_apk and rule.allows_apk():
            for m in fresh:
                if is_apk(m) or m.photo or m.video or m.document:
                    await enqueue(Job(
                        kind=JobKind.COPY, chat_id=chat_id, message_id=m.id,
                        dest_id=dest,
                        payload={"source": "album_apk", "file": getattr(m.document, "file_name", None),
                                 "silent": rule.silent},
                    ))
        elif keys and rule.allows_keys():
            for pname, key in keys:
                if await mark_key_async(key):
                    await enqueue(Job(
                        kind=JobKind.KEY, chat_id=chat_id, message_id=fresh[0].id,
                        dest_id=dest,
                        payload={"key": key, "pattern": pname, "album": True, "silent": rule.silent},
                    ))
        elif rule.allows_copy_generic():
            await enqueue(Job(
                kind=JobKind.COPY, chat_id=chat_id, message_id=fresh[0].id,
                dest_id=dest, payload={"source": "album", "silent": rule.silent},
            ))


async def handle_album_piece(client: Client, message: Message):
    key = (message.chat.id, str(message.media_group_id))
    async with album_lock:
        bucket = albums.get(key)
        if bucket is None:
            bucket = AlbumBucket()
            albums[key] = bucket
            bucket.task = asyncio.create_task(
                flush_album(client, message.chat.id, str(message.media_group_id))
            )
        bucket.messages.append(message)


# =====================================================
# 11. WORKERS
# =====================================================
async def worker(client: Client, wid: int):
    log.info("Worker-%s armed", wid)
    _metrics["workers_alive"] += 1
    try:
        while True:
            job: Job = await job_queue.get()
            try:
                await execute_job(client, job)
            except Exception as e:
                state.total_errors += 1
                log.exception("Worker-%s fail: %s", wid, e)
                await alert_error(f"worker-{wid}", str(e))
            finally:
                job_queue.task_done()
                _metrics["queue_depth"] = job_queue.qsize()
    finally:
        _metrics["workers_alive"] -= 1


async def execute_job(client: Client, job: Job):
    silent = bool(job.payload.get("silent"))

    if job.kind == JobKind.COPY:
        async def _do():
            try:
                await client.copy_message(
                    chat_id=job.dest_id,
                    from_chat_id=job.chat_id,
                    message_id=job.message_id,
                )
            except RPCError:
                await client.forward_messages(
                    chat_id=job.dest_id,
                    from_chat_id=job.chat_id,
                    message_ids=job.message_id,
                )

        await with_flood_retry(_do, label=f"COPY→{job.dest_id}")
        src = job.payload.get("source", "")
        if src in ("apk", "album_apk"):
            state.total_apks += 1
            fname = job.payload.get("file") or "unknown.apk"
            await alert_apk(job.chat_id, job.message_id, str(fname), job.dest_id, silent)
        state.last_hit_iso = datetime.now(timezone.utc).isoformat()
        log.info("COPY ok %s:%s → %s", job.chat_id, job.message_id, job.dest_id)

    elif job.kind == JobKind.KEY:
        key = job.payload["key"]
        pattern = job.payload.get("pattern", "?")
        album_tag = " · album" if job.payload.get("album") else ""
        body = (
            f"🔥 <b>New Key Detected</b>{html.escape(album_tag)}

"
            f"<code>{html.escape(key)}</code>

"
            f"pattern: <code>{html.escape(pattern)}</code>
"
            f"src: <code>{job.chat_id}</code> · msg: <code>{job.message_id}</code>
"
            f"<i>C2-EXTREME auto-grab</i>"
        )

        async def _do():
            await client.send_message(job.dest_id, body)

        await with_flood_retry(_do, label=f"KEY→{job.dest_id}")
        state.total_keys += 1
        state.last_hit_iso = datetime.now(timezone.utc).isoformat()
        await alert_key(job.chat_id, job.message_id, key, pattern, job.dest_id, silent)
        log.info("KEY [%s] %s… → %s", pattern, key[:12], job.dest_id)

    persist()


# =====================================================
# 12. TELEGRAM CLIENT + CONTROL PLANE
# =====================================================
app = Client(
    "shadow_bot",
    session_string=CFG.SESSION_STRING,
    api_id=CFG.API_ID,
    api_hash=CFG.API_HASH,
    sleep_threshold=CFG.SLEEP_THRESHOLD,
    workers=min(16, CFG.WORKERS * 2),
)

CMD_PREFIX = re.compile(
    rf"^{re.escape(CFG.SECRET_KEY)}s+(w+)(?:s+(.+))?$",
    re.I | re.S,
)


async def _resolve_chat(client: Client, target: str):
    raw = target.strip()
    try:
        if re.fullmatch(r"-?d+", raw):
            return await client.get_chat(int(raw))
        return await client.get_chat(raw)
    except Exception as e:
        if re.fullmatch(r"-?d+", raw):
            cid = int(raw)
            async for dialog in client.get_dialogs(limit=200):
                if dialog.chat and dialog.chat.id == cid:
                    return dialog.chat
        raise RuntimeError(f"Cannot resolve `{raw}`: {e}") from e


@app.on_message(filters.me & filters.text)
async def control_plane(client: Client, message: Message):
    text = (message.text or "").strip()
    m = CMD_PREFIX.match(text)
    if not m:
        return

    cmd = m.group(1).upper()
    arg = (m.group(2) or "").strip()
    global engine_paused

    try:
        if cmd == "ADD":
            target = arg.split()[0] if arg else ""
            if not target:
                return await message.reply_text("❌ `SECRET ADD <-100id|@user> [mode]`")
            parts = arg.split()
            chat = await _resolve_chat(client, parts[0])
            active_channels.add(chat.id)
            # optional mode on add: ADD -100x apk
            if len(parts) > 1 and parts[1].lower() in VALID_MODES:
                channel_rules[chat.id] = ChannelRule(mode=parts[1].lower())
            elif chat.id not in channel_rules:
                channel_rules[chat.id] = ChannelRule(mode="all")
            persist()
            rule = get_rule(chat.id)
            await message.reply_text(
                f"✅ **Target Locked**
"
                f"Title: `{chat.title or chat.first_name or '?'}`
"
                f"ID: `{chat.id}`
"
                f"Mode: `{rule.mode}`
"
                f"Active: `{len(active_channels)}`"
            )

        elif cmd in ("REMOVE", "RM"):
            cid = int(arg.split()[0])
            active_channels.discard(cid)
            channel_rules.pop(cid, None)
            persist()
            await message.reply_text(f"🗑️ Removed `{cid}`")

        elif cmd == "LIST":
            if not active_channels:
                return await message.reply_text("📭 No targets.")
            lines = []
            for c in sorted(active_channels):
                r = get_rule(c)
                extra = f" mode=`{r.mode}`"
                if r.dest_override:
                    extra += f" dest={r.dest_override}"
                if r.keywords_any:
                    extra += f" kw={r.keywords_any}"
                lines.append(f"• `{c}`{extra}")
            dests = "
".join(f"• `{d}`" for d in destinations)
            await message.reply_text(
                "**Targets**
" + "
".join(lines) + "

**Destinations**
" + dests
            )

        elif cmd == "DEST":
            parts = arg.split()
            if not parts:
                return await message.reply_text("❌ `DEST ADD|RM|LIST …`")
            sub = parts[0].upper()
            if sub == "ADD" and len(parts) > 1:
                chat = await _resolve_chat(client, parts[1])
                if chat.id not in destinations:
                    destinations.append(chat.id)
                    persist()
                await message.reply_text(f"✅ Dest `{chat.id}`")
            elif sub in ("RM", "REMOVE") and len(parts) > 1:
                did = int(parts[1])
                if did == CFG.MY_CHANNEL_ID:
                    return await message.reply_text("⚠️ Primary dest locked.")
                if did in destinations:
                    destinations.remove(did)
                    persist()
                await message.reply_text(f"🗑️ Dest `{did}`")
            else:
                await message.reply_text("Dests:
" + "
".join(f"`{d}`" for d in destinations))

        # ----- RULE <chat_id> <subcmd> ... -----
        elif cmd == "RULE":
            # RULE -100x MODE apk
            # RULE -100x KW add leak,premium
            # RULE -100x DENY add spam
            # RULE -100x DEST -100y,-100z   (override; empty = clear)
            # RULE -100x MINKEY 20
            # RULE -100x SILENT on|off
            # RULE -100x SHOW
            # RULE -100x RESET
            parts = arg.split(maxsplit=2)
            if len(parts) < 2:
                return await message.reply_text(
                    "❌ `RULE <chat_id> MODE|KW|DENY|DEST|MINKEY|SILENT|SHOW|RESET …`"
                )
            cid = int(parts[0])
            sub = parts[1].upper()
            rest = parts[2] if len(parts) > 2 else ""
            rule = channel_rules.get(cid) or ChannelRule()

            if sub == "MODE":
                mode = rest.strip().lower()
                if mode not in VALID_MODES:
                    return await message.reply_text(f"❌ modes: {', '.join(sorted(VALID_MODES))}")
                rule.mode = mode
                channel_rules[cid] = rule
                if cid not in active_channels:
                    active_channels.add(cid)
                persist()
                await message.reply_text(f"✅ `{cid}` mode → `{mode}`")

            elif sub == "KW":
                # KW add a,b | KW clear | KW list
                bits = rest.split(maxsplit=1)
                op = (bits[0] if bits else "list").lower()
                if op == "clear":
                    rule.keywords_any = []
                elif op == "add" and len(bits) > 1:
                    for k in bits[1].split(","):
                        k = k.strip()
                        if k and k not in rule.keywords_any:
                            rule.keywords_any.append(k)
                elif op == "rm" and len(bits) > 1:
                    rm = {x.strip().lower() for x in bits[1].split(",")}
                    rule.keywords_any = [k for k in rule.keywords_any if k.lower() not in rm]
                channel_rules[cid] = rule
                persist()
                await message.reply_text(f"KW `{cid}`: `{rule.keywords_any}`")

            elif sub == "DENY":
                bits = rest.split(maxsplit=1)
                op = (bits[0] if bits else "list").lower()
                if op == "clear":
                    rule.keywords_deny = []
                elif op == "add" and len(bits) > 1:
                    for k in bits[1].split(","):
                        k = k.strip()
                        if k and k not in rule.keywords_deny:
                            rule.keywords_deny.append(k)
                elif op == "rm" and len(bits) > 1:
                    rm = {x.strip().lower() for x in bits[1].split(",")}
                    rule.keywords_deny = [k for k in rule.keywords_deny if k.lower() not in rm]
                channel_rules[cid] = rule
                persist()
                await message.reply_text(f"DENY `{cid}`: `{rule.keywords_deny}`")

            elif sub == "DEST":
                if not rest.strip():
                    rule.dest_override = []
                else:
                    rule.dest_override = [int(x.strip()) for x in rest.split(",") if x.strip()]
                channel_rules[cid] = rule
                persist()
                await message.reply_text(f"DEST override `{cid}`: `{rule.dest_override or 'global'}`")

            elif sub == "MINKEY":
                rule.min_key_len = max(8, int(rest.strip()))
                channel_rules[cid] = rule
                persist()
                await message.reply_text(f"MINKEY `{cid}` → `{rule.min_key_len}`")

            elif sub == "SILENT":
                val = rest.strip().lower()
                rule.silent = val in ("1", "on", "true", "yes")
                channel_rules[cid] = rule
                persist()
                await message.reply_text(f"SILENT `{cid}` → `{rule.silent}`")

            elif sub == "SHOW":
                r = get_rule(cid)
                await message.reply_text(
                    f"**Rule `{cid}`**
```json
{json.dumps(asdict(r), indent=2)}
```"
                )

            elif sub == "RESET":
                channel_rules[cid] = ChannelRule()
                persist()
                await message.reply_text(f"♻️ Rule `{cid}` reset → mode=all")

            else:
                await message.reply_text("❌ Unknown RULE subcmd")

        elif cmd == "PAUSE":
            engine_paused = True
            persist()
            await message.reply_text("⏸️ PAUSED")

        elif cmd == "RESUME":
            engine_paused = False
            persist()
            await message.reply_text("▶️ RESUMED")

        elif cmd in ("STATUS", "STAT"):
            up = int(time.time() - _boot_ts)
            await message.reply_text(
                f"📡 **C2-EXTREME v4**
"
                f"State: `{'PAUSED' if engine_paused else 'ARMED'}`
"
                f"Uptime: `{up}s` | Boots: `{state.boot_count}`
"
                f"Targets: `{len(active_channels)}` | Rules: `{len(channel_rules)}`
"
                f"Dests: `{len(destinations)}`
"
                f"Queue: `{_metrics['queue_depth']}` | Workers: `{_metrics['workers_alive']}`
"
                f"Redis: `{'ON' if redis_ok else 'OFF'}` | Webhook: `{'ON' if CFG.WEBHOOK_URL else 'OFF'}`
"
                f"Intercepts: `{state.total_intercepts}` | APKs: `{state.total_apks}` | Keys: `{state.total_keys}`
"
                f"Errors: `{state.total_errors}` | Alerts: `{state.total_alerts}` | Floods: `{_metrics['flood_waits']}`
"
                f"Last hit: `{state.last_hit_iso or '—'}`"
            )

        elif cmd == "CLEARDEDUP":
            processed_msgs.clear(); processed_set.clear()
            seen_keys.clear(); seen_keys_set.clear()
            n1 = await redis_flush_prefix("msg")
            n2 = await redis_flush_prefix("key")
            await message.reply_text(f"🧹 Dedup wiped | redis msg={n1} key={n2}")

        elif cmd == "TESTHOOK":
            await send_webhook(
                title="🧪 Test Alert",
                description="Webhook pipeline OK",
                color=0x3498DB,
                fields=[{"name": "ts", "value": datetime.now(timezone.utc).isoformat()}],
                event="test",
            )
            await message.reply_text("✅ Test webhook fired" if CFG.WEBHOOK_URL else "⚠️ WEBHOOK_URL not set")

        elif cmd == "HELP":
            await message.reply_text(
                f"**C2-EXTREME v4** — prefix `{CFG.SECRET_KEY}`

"
                f"`ADD <-id|@u> [mode]` — lock target
"
                f"`REMOVE <id>`
"
                f"`LIST` / `DEST ADD|RM|LIST`
"
                f"`RULE <id> MODE all|apk|keys|media|text|off`
"
                f"`RULE <id> KW add a,b` / `KW clear`
"
                f"`RULE <id> DENY add x` / `DENY clear`
"
                f"`RULE <id> DEST -100a,-100b` (empty=global)
"
                f"`RULE <id> MINKEY 20`
"
                f"`RULE <id> SILENT on|off`
"
                f"`RULE <id> SHOW|RESET`
"
                f"`PAUSE` `RESUME` `STATUS`
"
                f"`CLEARDEDUP` `TESTHOOK` `HELP`"
            )
        else:
            await message.reply_text(f"Unknown `{cmd}` — send `SECRET HELP`")

    except Exception as e:
        log.exception("Control error")
        await message.reply_text(f"❌ `{e}`")
        await alert_error("control_plane", str(e))


# ---------- INTERCEPTOR ----------
@app.on_message(filters.channel | filters.group | filters.private)
async def intercept_and_forward(client: Client, message: Message):
    try:
        chat = message.chat
        if not chat:
            return
        chat_id = chat.id
        if chat_id not in active_channels:
            return

        rule = get_rule(chat_id)
        if rule.mode == "off":
            return

        if message.media_group_id:
            await handle_album_piece(client, message)
            return

        if not await mark_msg_async(chat_id, message.id):
            return

        state.total_intercepts += 1
        if engine_paused:
            return

        dests = dests_for(chat_id)
        text_content = message.text or message.caption or ""

        # keyword gate (for any text-bearing payload)
        if text_content and not rule.text_ok(text_content):
            return

        # A) APK
        if is_apk(message) and rule.allows_apk():
            fname = (message.document.file_name if message.document else "?") or "?"
            log.info("APK %s from %s", fname, chat_id)
            for dest in dests:
                await enqueue(Job(
                    kind=JobKind.COPY, chat_id=chat_id, message_id=message.id,
                    dest_id=dest,
                    payload={"source": "apk", "file": fname, "silent": rule.silent},
                ))
            return

        # B) Keys
        if text_content and rule.allows_keys():
            keys = extract_keys(text_content)
            for pname, key in keys:
                if len(key) < rule.min_key_len:
                    continue
                if not await mark_key_async(key):
                    continue
                for dest in dests:
                    await enqueue(Job(
                        kind=JobKind.KEY, chat_id=chat_id, message_id=message.id,
                        dest_id=dest,
                        payload={"key": key, "pattern": pname, "silent": rule.silent},
                    ))

    except Exception as e:
        state.total_errors += 1
        log.exception("Interceptor: %s", e)
        await alert_error("intercept", str(e))


# =====================================================
# 13. BOOT
# =====================================================
async def on_startup(client: Client):
    global job_queue, rate_lock, album_lock
    job_queue = asyncio.Queue(maxsize=CFG.QUEUE_MAX)
    rate_lock = asyncio.Lock()
    album_lock = asyncio.Lock()

    await redis_connect()
    _metrics["redis"] = redis_ok

    for cid in list(active_channels):
        try:
            chat = await client.get_chat(cid)
            log.info("Warm peer: %s (%s)", chat.title, cid)
        except Exception as e:
            log.warning("Warm fail %s: %s", cid, e)

    for i in range(CFG.WORKERS):
        asyncio.create_task(worker(client, i + 1), name=f"worker-{i+1}")

    me = await client.get_me()
    log.info(
        "C2-EXTREME v4 ONLINE as %s | targets=%s redis=%s webhook=%s",
        me.first_name, len(active_channels), redis_ok, bool(CFG.WEBHOOK_URL),
    )
    if CFG.ALERT_ON_BOOT:
        await send_webhook(
            title="🚀 Engine Boot",
            description=f"Online as **{me.first_name}**",
            color=0x9B59B6,
            fields=[
                {"name": "Targets", "value": str(len(active_channels))},
                {"name": "Redis", "value": "ON" if redis_ok else "OFF"},
                {"name": "Boot#", "value": str(state.boot_count)},
            ],
            event="boot",
        )


def _install_signals():
    def _handler(sig, frame):
        log.info("Signal %s — persist", sig)
        persist()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _handler)
        except Exception:
            pass


if __name__ == "__main__":
    print("=" * 56)
    print("  C2-EXTREME v4  ·  Redis · Rules · Webhooks")
    print("=" * 56)
    try:
        _ = CFG.API_ID, CFG.API_HASH, CFG.SESSION_STRING, CFG.MY_CHANNEL_ID
    except Exception as e:
        print(f"[-] CRITICAL BOOT: {e}")
        raise SystemExit(1)

    _install_signals()

    @app.on_raw_update()
    async def _once_boot(client, update, users, chats):
        if not getattr(_once_boot, "_done", False):
            _once_boot._done = True
            await on_startup(client)

    try:
        app.run()
    finally:
        persist()
        # close aiohttp if used
        try:
            if http_session and not http_session.closed:
                asyncio.get_event_loop().run_until_complete(http_session.close())
        except Exception:
            pass
        try:
            if redis_client is not None:
                asyncio.get_event_loop().run_until_complete(redis_client.aclose())
        except Exception:
            pass
