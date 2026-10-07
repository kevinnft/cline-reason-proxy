#!/usr/bin/env python3
"""OpenAI-compatible Cline proxy with API-key auth + reasoning remap.

Cline VSCode reads delta.reasoning / include_reasoning.
Hermes / OpenAI clients read delta.reasoning_content.
This proxy:
  - requires Bearer PROXY_API_KEY on /v1
  - UI login with the same API key, session cookie HttpOnly
  - round-robins Cline keys from 9router sqlite
  - injects include_reasoning=true
  - copies reasoning / reasoning_details -> reasoning_content
  - unwraps {data, success} envelopes
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import sqlite3
import sys
import threading
import time
import traceback
import http.client
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen


class _NoDelayHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        super().connect()
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class _NoDelayHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass


class _NoDelayHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_NoDelayHTTPConnection, req)


class _NoDelayHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_NoDelayHTTPSConnection, req)

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"


def default_nine_db() -> Path:
    override = os.environ.get("NINE_ROUTER_DB")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "9router" / "db" / "data.sqlite"
    return Path.home() / ".9router" / "db" / "data.sqlite"


NINE_DB = default_nine_db()
KEYS_FILE = Path(os.environ.get("CLINE_KEYS_FILE", str(ROOT / "keys.json"))).expanduser()
UPSTREAM = "https://api.cline.bot/api/v1/chat/completions"
MODELS_URL = "https://api.cline.bot/api/v1/models"
PORT = int(os.environ.get("CLINE_PROXY_PORT", "20129"))
HOST = os.environ.get("CLINE_PROXY_HOST", "127.0.0.1")

# Old picker names still remap; /v1/models lists the config catalog only.
MODEL_ALIASES = {
    "cline-glm-5.3-flash": "z-ai/glm-5.3-flash",
    "cline/z-ai/glm-5.3-flash": "z-ai/glm-5.3-flash",
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
    "cline-ds-v4-flash": "deepseek/deepseek-v4-flash",
    "cline/deepseek/deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    # Muse Spark 1.3 Contributor
    "muse-spark-1.3-contributor": "cline-free/muse-spark-1.3-contributor",
    "meta/muse-spark-1.3-contributor": "cline-free/muse-spark-1.3-contributor",
    "cline/muse-spark-1.3-contributor": "cline-free/muse-spark-1.3-contributor",
    "cline/meta/muse-spark-1.3-contributor": "cline-free/muse-spark-1.3-contributor",
    "muse-spark-1.3": "cline-free/muse-spark-1.3-contributor",
    "meta/muse-spark-1.3": "cline-free/muse-spark-1.3-contributor",
    # Moonshot Kimi K3 Free
    "kimi-k3": "cline-free/kimi-k3",
    "moonshotai/kimi-k3": "cline-free/kimi-k3",
    "moonshot/kimi-k3": "cline-free/kimi-k3",
    "cline/kimi-k3": "cline-free/kimi-k3",
    "cline/moonshotai/kimi-k3": "cline-free/kimi-k3",
    "cline-free/kimi-k3": "cline-free/kimi-k3",
    "cline-free/moonshotai/kimi-k3": "cline-free/kimi-k3",
    # Xiaomi Mimo v2.6 Flash Free
    "mimo-v2.6-flash": "cline-free/mimo-v2.6-flash",
    "xiaomi/mimo-v2.6-flash": "cline-free/mimo-v2.6-flash",
    "cline/mimo-v2.6-flash": "cline-free/mimo-v2.6-flash",
    "cline/xiaomi/mimo-v2.6-flash": "cline-free/mimo-v2.6-flash",
    "cline-mimo-v2.6-flash": "cline-free/mimo-v2.6-flash",
    "cline-free/mimo-v2.6-flash": "cline-free/mimo-v2.6-flash",
    "cline-free/xiaomi/mimo-v2.6-flash": "cline-free/mimo-v2.6-flash",
    # Stealth Space Bunny Alpha (free, cost 0)
    "space-bunny-alpha": "stealth/space-bunny-alpha",
    "stealth/space-bunny-alpha": "stealth/space-bunny-alpha",
    "cline/space-bunny-alpha": "stealth/space-bunny-alpha",
    "cline/stealth/space-bunny-alpha": "stealth/space-bunny-alpha",
    "cline-free/space-bunny-alpha": "stealth/space-bunny-alpha",
    "cline-free/stealth/space-bunny-alpha": "stealth/space-bunny-alpha",
    # Google Gemini 3.8 Flash Free
    "gemini-3.8-flash": "cline-free/gemini-3.8-flash",
    "google/gemini-3.8-flash": "cline-free/gemini-3.8-flash",
    "cline-gemini-3.8-flash": "cline-free/gemini-3.8-flash",
    "cline/gemini-3.8-flash": "cline-free/gemini-3.8-flash",
    "cline/google/gemini-3.8-flash": "cline-free/gemini-3.8-flash",
    "cline-free/gemini-3.8-flash": "cline-free/gemini-3.8-flash",
    "cline-free/google/gemini-3.8-flash": "cline-free/gemini-3.8-flash",
}

FALLBACK_MODEL = "deepseek/deepseek-v4-flash"
BUILTIN_MODELS = [
    "deepseek/deepseek-v4-flash",
    "cline-free/muse-spark-1.3-contributor",
    "cline-free/kimi-k3",
    "cline-free/mimo-v2.6-flash",
    "stealth/space-bunny-alpha",
]
RETRY_STATUSES = {401, 402, 403, 408, 409, 429, 500, 502, 503, 504}
MAX_FAILOVER = 32

_lock = threading.Lock()
_rr = 0
_own_keys: list[str] = []
_keys: list[str] = _own_keys
_nine_keys: list[str] = []
_proxy_key = ""
_default_effort = "max"
_model_efforts: dict[str, str] = {}
_public_models: list[str] = []
_proxy_enabled = False
_proxies: list[str] = []
_nine_proxies: list[str] = []
_proxy_rr = 0
_reasoning_required: set[str] = set()
WEB_DIR = ROOT / "web"
PAID_MODELS: set[str] = set()
PROXIES_FILE = Path(os.environ.get("CLINE_PROXIES_FILE", str(ROOT / "proxies.json"))).expanduser()
COOKIE_NAME = "crp_session"
SESSION_TTL = 30 * 24 * 3600
LOGIN_WINDOW = 900
LOGIN_MAX_FAILS = 20
STATS_DB = Path(os.environ.get("CLINE_STATS_DB", str(ROOT / "stats.sqlite"))).expanduser()
STATS_KEEP_SEC = 30 * 24 * 3600
_sessions: dict[str, float] = {}
_login_fails: dict[str, list[float]] = {}
_stats_q: queue.Queue = queue.Queue(maxsize=4096)
_stats_cache: dict[str, Any] = {"at": 0.0, "payload": None}


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def sd_notify(state: str) -> None:
    """Talk to systemd (READY / WATCHDOG). No-op if not under systemd."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    try:
        import socket as _socket

        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        addr: str | bytes
        if path.startswith("@"):
            addr = "\0" + path[1:]
        else:
            addr = path
        sock.connect(addr)
        sock.sendall(state.encode("utf-8"))
        sock.close()
    except OSError:
        pass


def watchdog_loop() -> None:
    usec = int(os.environ.get("WATCHDOG_USEC") or "0")
    interval = max(1.0, (usec / 1_000_000) / 3) if usec else 5.0
    while True:
        sd_notify("WATCHDOG=1")
        time.sleep(interval)


def _stats_connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(STATS_DB), timeout=2, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def init_stats_db() -> None:
    con = _stats_connect()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS hits (
                 ts INTEGER NOT NULL,
                 model TEXT NOT NULL DEFAULT '',
                 stream INTEGER NOT NULL DEFAULT 0,
                 ok INTEGER NOT NULL DEFAULT 0,
                 prompt INTEGER NOT NULL DEFAULT 0,
                 completion INTEGER NOT NULL DEFAULT 0,
                 total INTEGER NOT NULL DEFAULT 0,
                 effort TEXT NOT NULL DEFAULT '',
                 key_tail TEXT NOT NULL DEFAULT '',
                 proxy TEXT NOT NULL DEFAULT '',
                 key_n INTEGER NOT NULL DEFAULT 0,
                 proxy_n INTEGER NOT NULL DEFAULT 0
               )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS hits_ts ON hits(ts)")
        for col, typedef in (
            ("effort", "TEXT NOT NULL DEFAULT ''"),
            ("key_tail", "TEXT NOT NULL DEFAULT ''"),
            ("proxy", "TEXT NOT NULL DEFAULT ''"),
            ("key_n", "INTEGER NOT NULL DEFAULT 0"),
            ("proxy_n", "INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                con.execute(f"ALTER TABLE hits ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
    finally:
        con.close()


def _int_usage(obj: Any, *keys: str) -> int:
    if not isinstance(obj, dict):
        return 0
    for k in keys:
        v = obj.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return 0


def extract_usage(obj: Any) -> tuple[int, int, int]:
    if not isinstance(obj, dict):
        return 0, 0, 0
    raw_u = obj.get("usage")
    u: dict[str, Any] = raw_u if isinstance(raw_u, dict) else {}
    prompt = _int_usage(u, "prompt_tokens", "input_tokens", "promptTokens")
    completion = _int_usage(
        u, "completion_tokens", "output_tokens", "completionTokens"
    )
    total = _int_usage(u, "total_tokens", "totalTokens")
    if total <= 0:
        total = prompt + completion
    raw_d = u.get("completion_tokens_details")
    details: dict[str, Any] = raw_d if isinstance(raw_d, dict) else {}
    reasoning = _int_usage(details, "reasoning_tokens", "reasoningTokens")
    if reasoning and completion and completion < reasoning:
        completion += reasoning
        total = prompt + completion
    return prompt, completion, total


def _sse_usage(line: bytes) -> dict[str, Any] | None:
    raw = line.rstrip(b"\r")
    if not raw.startswith(b"data:"):
        return None
    payload = raw[5:].strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        obj = json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        return None
    obj = unwrap_body(obj)
    if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
        return obj
    return None


def record_hit(
    model: str,
    stream: bool,
    ok: bool,
    usage_obj: Any = None,
    prompt: int = 0,
    completion: int = 0,
    total: int = 0,
    effort: str = "",
    key_tail: str = "",
    proxy: str = "",
    key_n: int = 0,
    proxy_n: int = 0,
) -> None:
    if usage_obj is not None:
        prompt, completion, total = extract_usage(usage_obj)
    item = (
        int(time.time()),
        (model or "")[:80],
        1 if stream else 0,
        1 if ok else 0,
        max(0, int(prompt)),
        max(0, int(completion)),
        max(0, int(total)),
        (effort or "")[:32],
        (key_tail or "")[:12],
        (proxy or "")[:80],
        max(0, int(key_n)),
        max(0, int(proxy_n)),
    )
    try:
        _stats_q.put_nowait(item)
    except queue.Full:
        pass


def stats_writer_loop() -> None:
    con = _stats_connect()
    last_prune = 0.0
    pending: list[tuple] = []
    try:
        while True:
            try:
                item = _stats_q.get(timeout=0.4)
                pending.append(item)
            except queue.Empty:
                item = None
            if item is not None:
                while len(pending) < 64:
                    try:
                        pending.append(_stats_q.get_nowait())
                    except queue.Empty:
                        break
            if pending:
                con.executemany(
                    "INSERT INTO hits(ts,model,stream,ok,prompt,completion,total,effort,key_tail,proxy,key_n,proxy_n) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    pending,
                )
                pending.clear()
            now = time.time()
            if now - last_prune >= 3600:
                cutoff = int(now) - STATS_KEEP_SEC
                con.execute("DELETE FROM hits WHERE ts < ?", (cutoff,))
                last_prune = now
    except Exception as e:
        log(f"stats writer stop: {e}")
    finally:
        try:
            con.close()
        except Exception:
            pass


def empty_window() -> dict[str, int]:
    return {
        "requests": 0,
        "ok": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tokens": 0,
    }


def stats_payload() -> dict[str, Any]:
    now = time.time()
    cached = _stats_cache.get("payload")
    if cached is not None and now - float(_stats_cache.get("at") or 0) < 5:
        return cached
    windows = (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400))
    out: dict[str, Any] = {name: empty_window() for name, _ in windows}
    out["since"] = None
    out["recent"] = []
    try:
        con = sqlite3.connect(f"file:{STATS_DB}?mode=ro", uri=True, timeout=1)
        try:
            now_i = int(now)
            for name, span in windows:
                since = now_i - span
                row = con.execute(
                    "SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(SUM(prompt),0), "
                    "COALESCE(SUM(completion),0), COALESCE(SUM(total),0) FROM hits WHERE ts >= ?",
                    (since,),
                ).fetchone()
                out[name] = {
                    "requests": int(row[0] or 0),
                    "ok": int(row[1] or 0),
                    "prompt_tokens": int(row[2] or 0),
                    "completion_tokens": int(row[3] or 0),
                    "tokens": int(row[4] or 0),
                }
            first = con.execute("SELECT MIN(ts) FROM hits").fetchone()[0]
            out["since"] = int(first) if first else None
            rows = con.execute(
                "SELECT ts, model, stream, ok, prompt, completion, total, COALESCE(effort, ''), "
                "COALESCE(key_tail, ''), COALESCE(proxy, ''), COALESCE(key_n, 0), COALESCE(proxy_n, 0) "
                "FROM hits ORDER BY ts DESC LIMIT 40"
            ).fetchall()
            out["recent"] = [
                {
                    "ts": int(r[0]),
                    "model": r[1],
                    "stream": bool(r[2]),
                    "ok": bool(r[3]),
                    "prompt_tokens": int(r[4] or 0),
                    "completion_tokens": int(r[5] or 0),
                    "tokens": int(r[6] or 0),
                    "effort": str(r[7] or ""),
                    "key": str(r[8] or ""),
                    "proxy": str(r[9] or ""),
                    "key_n": int(r[10] or 0),
                    "proxy_n": int(r[11] or 0),
                }
                for r in rows
            ]
        finally:
            con.close()
    except Exception:
        pass
    _stats_cache["at"] = now
    _stats_cache["payload"] = out
    return out


def load_or_create_config() -> dict:
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    else:
        cfg = {}
    if not cfg.get("api_key"):
        cfg["api_key"] = "sk-crp-" + secrets.token_urlsafe(24)
    cfg.setdefault("port", PORT)
    cfg.setdefault("host", HOST)
    cfg.setdefault("reasoning_effort", "max")
    cfg.setdefault("proxy_enabled", False)
    if not isinstance(cfg.get("public_models"), list) or not cfg.get("public_models"):
        cfg["public_models"] = list(BUILTIN_MODELS)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return cfg


def _read_key_file() -> list[str]:
    """Own pool only. keys.json is a JSON list of sk_ strings, or {\"keys\": [...]}."""
    if not KEYS_FILE.is_file():
        return []
    blob = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    raw: list = []
    if isinstance(blob, list):
        raw = blob
    elif isinstance(blob, dict):
        raw = blob.get("keys") or blob.get("apiKeys") or []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            k = item.strip()
        elif isinstance(item, dict):
            k = str(item.get("apiKey") or item.get("key") or "").strip()
        else:
            k = ""
        if k:
            out.append(k)
    return out


def _write_key_file(keys: list[str]) -> None:
    KEYS_FILE.write_text(json.dumps(keys, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass


def _split_key_blob(text: str) -> list[str]:
    parts = text.replace(";", "\n").replace(",", "\n").splitlines()
    return [p.strip().strip("\"'") for p in parts if p.strip().strip("\"'")]


def _looks_like_cline_key(k: str) -> bool:
    return k.startswith("sk_") and 8 <= len(k) <= 400 and " " not in k


def load_own_keys() -> list[str]:
    """Local pool: CLINE_API_KEYS env, then keys.json. Never 9router."""
    keys: list[str] = []
    seen: set[str] = set()

    def add(k: str) -> None:
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    env_keys = os.environ.get("CLINE_API_KEYS") or os.environ.get("CLINE_API_KEY") or ""
    for part in _split_key_blob(env_keys):
        add(part)
    for k in _read_key_file():
        add(k)
    return keys


def load_nine_keys() -> list[str]:
    """9router sqlite, kept only so the dashboard can import it. Not in the live pool."""
    db = default_nine_db()
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT data FROM providerConnections WHERE provider='cline' AND isActive=1"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    active: list[str] = []
    rest: list[str] = []
    seen: set[str] = set()
    for (blob,) in rows:
        try:
            d = json.loads(blob)
        except Exception:
            continue
        k = (d.get("apiKey") or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        if (d.get("testStatus") or "") == "active":
            active.append(k)
        else:
            rest.append(k)
    return active + rest


def load_cline_keys() -> list[str]:
    return load_own_keys()


def pool_view() -> dict:
    own = list(_own_keys)
    own_set = set(own)
    nine = [k for k in _nine_keys if k not in own_set]
    nxt = (_rr % len(own)) + 1 if own else 0
    return {
        "ok": True,
        "source": "keys.json",
        "count": len(own),
        "next": nxt,
        "keys": [{"n": i, "tag": key_tag(k), "tail": k[-6:]} for i, k in enumerate(own, 1)],
        "nine_available": len(nine),
    }


def _apply_own_keys(keys: list[str]) -> None:
    global _own_keys, _keys, _rr
    with _lock:
        _own_keys = keys
        _keys = list(keys)
        if _keys:
            _rr = _rr % len(_keys)


def add_pool_keys(text: str) -> dict:
    incoming = _split_key_blob(text or "")
    if not incoming:
        return {"ok": False, "error": "no keys in paste"}
    bad = [k[:12] for k in incoming if not _looks_like_cline_key(k)]
    if bad:
        return {"ok": False, "error": f"not a Cline sk_ key: {bad[0]}", "rejected": len(bad)}
    current = list(_own_keys)
    seen = set(current)
    added = 0
    for k in incoming:
        if k not in seen:
            seen.add(k)
            current.append(k)
            added += 1
    _write_key_file(current)
    _apply_own_keys(current)
    log(f"pool add +{added} total={len(current)}")
    out = pool_view()
    out["added"] = added
    out["skipped"] = len(incoming) - added
    return out


def remove_pool_key(tail: str) -> dict:
    tail = (tail or "").strip()
    if len(tail) < 4:
        return {"ok": False, "error": "tail too short"}
    hits = [k for k in _own_keys if k.endswith(tail)]
    if len(hits) != 1:
        return {"ok": False, "error": "key not found" if not hits else "tail matches more than one key"}
    current = [k for k in _own_keys if k != hits[0]]
    _write_key_file(current)
    _apply_own_keys(current)
    log(f"pool remove {key_tag(hits[0])} total={len(current)}")
    return pool_view()


def clear_pool() -> dict:
    _write_key_file([])
    _apply_own_keys([])
    log("pool cleared")
    return pool_view()


def import_nine_pool() -> dict:
    """One-shot copy of 9router Cline keys into the local pool. Does not keep the link."""
    incoming = load_nine_keys()
    if not incoming:
        return {"ok": False, "error": "no Cline keys in 9router sqlite"}
    return add_pool_keys("\n".join(incoming))


def key_tag(k: str) -> str:
    k = k or ""
    return f"…{k[-6:]}" if len(k) >= 6 else "…"


def key_index(k: str) -> int:
    try:
        return _keys.index(k) + 1
    except ValueError:
        return 0


def proxy_index(url: str | None) -> int:
    if not url:
        return 0
    try:
        return _proxies.index(url) + 1
    except ValueError:
        return 0


def next_key() -> str | None:
    """Consume exactly one key and advance the RR pointer."""
    global _rr
    with _lock:
        if not _keys:
            return None
        k = _keys[_rr % len(_keys)]
        _rr = (_rr + 1) % len(_keys)
        return k


def next_keys(n: int) -> list[str]:
    out: list[str] = []
    for _ in range(max(0, n)):
        k = next_key()
        if not k:
            break
        out.append(k)
    return out


def _add_proxy_url(url: str, seen: set[str], out: list[str]) -> None:
    u = (url or "").strip()
    if not u or u in seen:
        return
    p = urlparse(u)
    if p.scheme not in ("http", "https"):
        return
    if not p.hostname:
        return
    seen.add(u)
    out.append(u)


def load_own_proxies() -> list[str]:
    """Local egress list: CLINE_PROXIES env, then proxies.json. Never 9router."""
    out: list[str] = []
    seen: set[str] = set()
    env = os.environ.get("CLINE_PROXIES") or os.environ.get("CLINE_PROXY") or ""
    for part in env.replace(";", ",").split(","):
        _add_proxy_url(part, seen, out)
    for item in _read_proxy_file():
        _add_proxy_url(item, seen, out)
    return out


def _read_proxy_file() -> list[str]:
    if not PROXIES_FILE.is_file():
        return []
    blob = json.loads(PROXIES_FILE.read_text(encoding="utf-8"))
    raw: list = []
    if isinstance(blob, list):
        raw = blob
    elif isinstance(blob, dict):
        raw = blob.get("proxies") or blob.get("urls") or []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item.strip())
        elif isinstance(item, dict):
            out.append(str(item.get("proxyUrl") or item.get("url") or "").strip())
    return [u for u in out if u]


def _write_proxy_file(urls: list[str]) -> None:
    PROXIES_FILE.write_text(json.dumps(urls, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(PROXIES_FILE, 0o600)
    except OSError:
        pass


def load_nine_proxies() -> list[str]:
    """9router proxyPools, import-only. Not in the live egress list."""
    db = default_nine_db()
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT data FROM proxyPools WHERE isActive=1").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out: list[str] = []
    seen: set[str] = set()
    for (blob,) in rows:
        try:
            d = json.loads(blob)
        except Exception:
            continue
        _add_proxy_url(str(d.get("proxyUrl") or ""), seen, out)
    return out


def load_egress_proxies() -> list[str]:
    return load_own_proxies()


def proxy_view() -> dict:
    own = list(_proxies)
    own_set = set(own)
    nxt = (_proxy_rr % len(own)) + 1 if own else 0
    return {
        "ok": True,
        "enabled": _proxy_enabled,
        "count": len(own),
        "next": nxt,
        "proxies": [{"n": i, "tag": proxy_tag(u), "url": u} for i, u in enumerate(own, 1)],
        "nine_available": sum(1 for u in _nine_proxies if u not in own_set),
    }


def _apply_proxies(urls: list[str]) -> None:
    global _proxies, _proxy_rr
    with _lock:
        _proxies = urls
        if _proxies:
            _proxy_rr = _proxy_rr % len(_proxies)


def add_proxies(text: str) -> dict:
    incoming = [p.strip() for p in (text or "").replace(";", "\n").replace(",", "\n").splitlines() if p.strip()]
    if not incoming:
        return {"ok": False, "error": "no proxies in paste"}
    current = list(_proxies)
    seen = set(current)
    rejected: list[str] = []
    added = 0
    for raw in incoming:
        trial: list[str] = []
        _add_proxy_url(raw, set(), trial)
        if not trial:
            rejected.append(raw[:40])
            continue
        url = trial[0]
        if url not in seen:
            seen.add(url)
            current.append(url)
            added += 1
    if rejected and not added:
        return {"ok": False, "error": f"not an http(s) proxy url: {rejected[0]}", "rejected": len(rejected)}
    _write_proxy_file(current)
    _apply_proxies(current)
    log(f"proxy add +{added} total={len(current)}")
    out = proxy_view()
    out["added"] = added
    out["rejected"] = len(rejected)
    return out


def remove_proxy(tag: str) -> dict:
    tag = (tag or "").strip()
    hits = [u for u in _proxies if proxy_tag(u) == tag or u == tag]
    if len(hits) != 1:
        return {"ok": False, "error": "proxy not found" if not hits else "tag matches more than one proxy"}
    current = [u for u in _proxies if u != hits[0]]
    _write_proxy_file(current)
    _apply_proxies(current)
    log(f"proxy remove {proxy_tag(hits[0])} total={len(current)}")
    return proxy_view()


def import_nine_proxies() -> dict:
    incoming = load_nine_proxies()
    if not incoming:
        return {"ok": False, "error": "no active proxies in 9router sqlite"}
    return add_proxies("\n".join(incoming))


def proxy_tag(url: str) -> str:
    p = urlparse(url or "")
    host = p.hostname or "?"
    port = p.port or (443 if p.scheme == "https" else 80)
    return f"{host}:{port}"


def next_proxy() -> str | None:
    global _proxy_rr
    with _lock:
        if not _proxy_enabled or not _proxies:
            return None
        u = _proxies[_proxy_rr % len(_proxies)]
        _proxy_rr = (_proxy_rr + 1) % len(_proxies)
        return u


def body_kind(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").lower()
    if "reasoning is mandatory" in text:
        return "reasoning"
    if "empty response content" in text:
        return "empty"
    return ""


def note_reasoning_required(model: str) -> None:
    if model and model not in _reasoning_required:
        _reasoning_required.add(model)
        log(f"reasoning required model={model}")


def log_failed_shape(req_body: dict, kind: str) -> None:
    log(
        "failed shape "
        f"kind={kind} model={req_body.get('model')} stream={bool(req_body.get('stream'))} "
        f"effort={req_body.get('reasoning_effort')} "
        f"max_tokens={req_body.get('max_tokens')} "
        f"max_completion_tokens={req_body.get('max_completion_tokens')} "
        f"msgs={len(req_body.get('messages') or [])}"
    )


def open_upstream(req: Request, timeout: int = 180, proxy_url: str | None = None):
    if proxy_url:
        opener = build_opener(
            ProxyHandler({"http": proxy_url, "https": proxy_url}),
            _NoDelayHTTPHandler(),
            _NoDelayHTTPSHandler(),
        )
        return opener.open(req, timeout=timeout)
    return urlopen(req, timeout=timeout)


def cline_headers(api_key: str) -> dict[str, str]:
    # 9router Cline pool stores app.cline.bot API keys (sk_...), not WorkOS
    # account tokens. Prefixing workos: → 401 "re-authenticate / latest version".
    # Docs: Authorization: Bearer YOUR_API_KEY
    # https://github.com/cline/cline/blob/main/docs/api/authentication.mdx
    token = api_key.strip()
    if token.startswith("workos:"):
        rest = token[7:]
        if rest.startswith("sk_"):
            token = rest
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "HTTP-Referer": "https://cline.bot",
        "X-Title": "Cline",
        "User-Agent": "cline/4.1.16",
        "X-PLATFORM": "win32" if os.name == "nt" else sys.platform,
        "X-PLATFORM-VERSION": os.environ.get("CLINE_PLATFORM_VERSION") or ("1.105.0" if os.name == "nt" else sys.version.split()[0]),
        "X-CLIENT-TYPE": "VS Code",
        "X-CLIENT-VERSION": "4.1.16",
        "X-CORE-VERSION": "4.1.16",
        "X-IS-MULTIROOT": "false",
    }


def default_model() -> str:
    if _public_models:
        return _public_models[0]
    return FALLBACK_MODEL


def resolve_model(name: str | None) -> str:
    if not name:
        return default_model()
    return MODEL_ALIASES.get(name, name)


def reasoning_text_from_obj(obj: Any) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for k in ("text", "content", "summary", "thinking", "reasoning"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
        return ""
    if isinstance(obj, list):
        parts = [reasoning_text_from_obj(x) for x in obj]
        return "".join(p for p in parts if p)
    return ""


def inject_reasoning_fields(node: dict) -> None:
    """Copy reasoning / reasoning_details onto reasoning_content in-place."""
    if not isinstance(node, dict):
        return
    existing = node.get("reasoning_content")
    if not (isinstance(existing, str) and existing):
        text = ""
        r = node.get("reasoning")
        if isinstance(r, str) and r:
            text = r
        elif r:
            text = reasoning_text_from_obj(r)
        if not text:
            text = reasoning_text_from_obj(node.get("reasoning_details"))
        if text:
            node["reasoning_content"] = text
    # recurse common OpenAI shapes
    for key in ("message", "delta"):
        child = node.get(key)
        if isinstance(child, dict):
            inject_reasoning_fields(child)
    ch = node.get("choices")
    if isinstance(ch, list):
        for c in ch:
            if isinstance(c, dict):
                inject_reasoning_fields(c)


def unwrap_body(obj: Any) -> Any:
    if isinstance(obj, dict) and "data" in obj and (
        obj.get("success") is True or "choices" in (obj.get("data") or {})
    ):
        inner = obj["data"]
        if isinstance(inner, dict):
            return inner
    return obj


def normalize_completion(obj: Any) -> Any:
    obj = unwrap_body(obj)
    if isinstance(obj, dict):
        inject_reasoning_fields(obj)
        if "object" not in obj and "choices" in obj:
            obj["object"] = "chat.completion"
    return obj


VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}

EFFORT_MAP = {
    "extra-high": "max",
    "extra_high": "max",
    "ultra": "max",
}


def _norm_effort(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    v = val.strip().lower()
    if v in EFFORT_MAP:
        return EFFORT_MAP[v]
    if v in VALID_EFFORTS:
        return v
    return None


def prepare_request_body(body: dict) -> dict:
    body = dict(body)
    model = resolve_model(body.get("model"))
    body["model"] = model
    if "include_reasoning" not in body:
        body["include_reasoning"] = True
    # Cline VSCode sets this; keep if client sent, else enable
    if body.get("stream") and "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    # Effort values: none|minimal|low|medium|high|xhigh|max
    effort = None
    r = body.get("reasoning")
    if isinstance(r, dict) and r.get("effort"):
        effort = _norm_effort(r.get("effort"))
    if effort is None:
        effort = _norm_effort(body.get("reasoning_effort"))
    if not effort:
        effort = _model_efforts.get(model) or _default_effort or "max"
    # Learned from an upstream "Reasoning is mandatory" body, plus the known ids.
    if effort == "none" and (model in _reasoning_required or "muse-spark" in model or "space-bunny" in model):
        effort = "low"
    # Meta muse-spark rejects effort='max' with 400 (Supported: minimal, low, medium, high, xhigh)
    if "muse-spark" in model and effort == "max":
        effort = "xhigh"
    # Any reasoning request (effort != 'none') requires headroom so thinking tokens
    # do not exhaust max_tokens before producing content (triggers upstream 500 "empty response content").
    if effort and effort != "none":
        if isinstance(body.get("max_tokens"), int) and body["max_tokens"] < 256:
            body["max_tokens"] = 256
        if isinstance(body.get("max_completion_tokens"), int) and body["max_completion_tokens"] < 256:
            body["max_completion_tokens"] = 256
    body["reasoning"] = {**(r if isinstance(r, dict) else {}), "effort": effort}
    body["reasoning_effort"] = effort
    return body


def listed_models() -> list[dict]:
    out = []
    seen = set()
    for mid in _public_models:
        real = resolve_model(mid)
        if mid in seen:
            continue
        seen.add(mid)
        out.append({
            "id": mid,
            "root": real,
            "object": "model",
            "owned_by": "cline-proxy",
            "paid": mid in PAID_MODELS or real in PAID_MODELS,
        })
    return out


def probe_model(model_id: str, timeout: int = 15) -> dict:
    """One non-stream ping through the same upstream path as /v1/chat/completions.
    Single key, no failover — a dashboard check should not burn the key pool."""
    model = resolve_model(model_id)
    if model not in _public_models and model_id not in _public_models:
        return {"ok": False, "model": model, "status": 404, "error": "model not in catalog"}
    body = prepare_request_body({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 64,
        "stream": False,
    })
    data = json.dumps(body).encode("utf-8")
    key = next_key()
    if not key:
        return {"ok": False, "model": model, "status": 503, "error": "no cline keys"}
    px = next_proxy()
    via = proxy_tag(px) if px else "direct"
    t0 = time.time()
    try:
        req = Request(UPSTREAM, data=data, headers=cline_headers(key), method="POST")
        with open_upstream(req, timeout=timeout, proxy_url=px) as resp:
            raw = resp.read()
            status = resp.status
    except HTTPError as e:
        raw = e.read() or b""
        ms = int((time.time() - t0) * 1000)
        err = raw[:240].decode("utf-8", "replace")
        log(f"probe fail model={model} key={key_tag(key)} via={via} status={e.code} {ms}ms")
        return {"ok": False, "model": model, "status": e.code, "ms": ms, "via": via, "error": err}
    except (URLError, TimeoutError, OSError) as e:
        ms = int((time.time() - t0) * 1000)
        log(f"probe fail model={model} key={key_tag(key)} via={via} net={e} {ms}ms")
        return {"ok": False, "model": model, "status": 502, "ms": ms, "via": via, "error": str(e)[:240]}
    ms = int((time.time() - t0) * 1000)
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        log(f"probe fail model={model} key={key_tag(key)} via={via} bad-json {ms}ms")
        return {"ok": False, "model": model, "status": status, "ms": ms, "via": via, "error": "non-json upstream"}
    obj = normalize_completion(obj)
    msg = ((obj.get("choices") or [{}])[0].get("message") or {})
    content = (msg.get("content") or "").strip()
    reasoning = bool(msg.get("reasoning_content") or msg.get("reasoning"))
    ok = status == 200 and bool(content)
    log(f"probe {'ok' if ok else 'empty'} model={model} key={key_tag(key)} via={via} status={status} {ms}ms")
    return {
        "ok": ok,
        "model": obj.get("model") or model,
        "status": status,
        "ms": ms,
        "via": via,
        "content": content[:160],
        "reasoning": reasoning,
        "effort": body.get("reasoning_effort"),
    }


def settings_payload() -> dict:
    models = listed_models()
    sample = [proxy_tag(u) for u in _proxies[:8]]
    return {
        "ok": True,
        "api_key": _proxy_key,
        "host": HOST,
        "port": PORT,
        "keys": len(_keys),
        "reasoning_effort": _default_effort,
        "model_reasoning_effort": _model_efforts,
        "include_reasoning": True,
        "default_model": default_model(),
        "models": models,
        "upstream": UPSTREAM,
        "proxy_enabled": _proxy_enabled,
        "proxies": len(_proxies),
        "proxy_sample": sample,
        "stats": stats_payload(),
    }


def persist_catalog() -> None:
    cfg = load_or_create_config()
    cfg["public_models"] = list(_public_models)
    cfg["model_reasoning_effort"] = dict(_model_efforts)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def add_catalog_model(raw: str) -> dict:
    global _public_models
    name = (raw or "").strip()
    if not name or len(name) > 120 or any(c.isspace() for c in name):
        return {"ok": False, "error": "invalid model id"}
    model = resolve_model(name)
    if model in _public_models:
        return {"ok": False, "error": "already in catalog", "model": model}
    _public_models.append(model)
    _model_efforts.setdefault(model, _default_effort or "max")
    persist_catalog()
    log(f"catalog add {model}")
    return settings_payload()


def remove_catalog_model(raw: str) -> dict:
    global _public_models
    name = (raw or "").strip()
    model = resolve_model(name)
    if model not in _public_models and name not in _public_models:
        return {"ok": False, "error": "not in catalog", "model": model}
    _public_models = [m for m in _public_models if m not in (model, name)]
    _model_efforts.pop(model, None)
    _model_efforts.pop(name, None)
    persist_catalog()
    log(f"catalog remove {model}")
    return settings_payload()


def save_config_patch(patch: dict) -> dict:
    global _default_effort, _proxy_key, _proxy_enabled, _model_efforts
    cfg = load_or_create_config()
    if "reasoning_effort" in patch:
        effort = _norm_effort(patch.get("reasoning_effort")) or "max"
        if effort not in VALID_EFFORTS:
            effort = "max"
        cfg["reasoning_effort"] = effort
        _default_effort = effort
    if "model_reasoning_effort" in patch and isinstance(patch["model_reasoning_effort"], dict):
        for m, eff in patch["model_reasoning_effort"].items():
            norm_eff = _norm_effort(eff)
            if norm_eff and norm_eff in VALID_EFFORTS:
                _model_efforts[resolve_model(m)] = norm_eff
        cfg["model_reasoning_effort"] = _model_efforts
    if "model" in patch and "effort" in patch:
        m = resolve_model(patch["model"])
        norm_eff = _norm_effort(patch["effort"])
        if norm_eff and norm_eff in VALID_EFFORTS:
            _model_efforts[m] = norm_eff
            cfg["model_reasoning_effort"] = _model_efforts
    if "proxy_enabled" in patch:
        _proxy_enabled = bool(patch.get("proxy_enabled"))
        cfg["proxy_enabled"] = _proxy_enabled
        log(f"egress proxy {'ON' if _proxy_enabled else 'OFF'} pool={len(_proxies)}")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return settings_payload()


def extract_bearer(handler: BaseHTTPRequestHandler) -> str:
    auth = handler.headers.get("Authorization") or handler.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (handler.headers.get("x-api-key") or "").strip()


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    # Socket peer only. X-Forwarded-For is untrusted: this process binds
    # 0.0.0.0:20129 with no reverse proxy in front.
    return handler.client_address[0] if handler.client_address else ""


def _cookie_value(handler: BaseHTTPRequestHandler, name: str) -> str:
    raw = handler.headers.get("Cookie") or ""
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return ""
    morsel = jar.get(name)
    return morsel.value if morsel else ""


def _purge_sessions(now: float) -> None:
    dead = [tok for tok, exp in _sessions.items() if exp <= now]
    for tok in dead:
        _sessions.pop(tok, None)


def session_ok(handler: BaseHTTPRequestHandler) -> bool:
    token = _cookie_value(handler, COOKIE_NAME)
    if not token:
        return False
    now = time.time()
    with _lock:
        _purge_sessions(now)
        exp = _sessions.get(token)
        if exp is None or exp <= now:
            _sessions.pop(token, None)
            return False
        _sessions[token] = now + SESSION_TTL
        return True


def issue_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + SESSION_TTL
    return token


def revoke_session(handler: BaseHTTPRequestHandler) -> None:
    token = _cookie_value(handler, COOKIE_NAME)
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


def session_cookie_header(token: str, max_age: int = SESSION_TTL) -> str:
    # No Secure flag: UI is served over plain HTTP on :20129.
    return (
        f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
    )


def login_allowed(ip: str) -> bool:
    now = time.time()
    with _lock:
        hits = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
        _login_fails[ip] = hits
        return len(hits) < LOGIN_MAX_FAILS


def login_fail(ip: str) -> None:
    with _lock:
        _login_fails.setdefault(ip, []).append(time.time())


def login_ok(ip: str) -> None:
    with _lock:
        _login_fails.pop(ip, None)


def key_matches(got: str) -> bool:
    if not got or not _proxy_key:
        return False
    a, b = got.encode("utf-8"), _proxy_key.encode("utf-8")
    if len(a) != len(b):
        secrets.compare_digest(a, a)
        return False
    return secrets.compare_digest(a, b)


def json_bytes(obj: Any, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, payload: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)
        try:
            self.wfile.flush()
        except Exception:
            pass

    def _err(self, status: int, msg: str, typ: str = "invalid_request_error") -> None:
        _, payload, ctype = json_bytes(
            {"error": {"message": msg, "type": typ, "code": status}}
        )
        self._send(status, payload, ctype)

    def _auth_ok(self) -> bool:
        got = extract_bearer(self)
        if not key_matches(got):
            self._err(401, "API key required. Set Authorization: Bearer <proxy api key>", "authentication_error")
            return False
        return True

    def _ui_ok(self) -> bool:
        if session_ok(self):
            return True
        if key_matches(extract_bearer(self)):
            return True
        return False

    def _send_html(self, path: Path, extra: dict | None = None) -> None:
        if not path.is_file():
            self._err(404, f"{path.name} missing")
            return
        self._send(200, path.read_bytes(), "text/html; charset=utf-8", extra)

    def _redirect(self, location: str, extra: dict | None = None) -> None:
        headers = {"Location": location}
        if extra:
            headers.update(extra)
        self._send(302, b"", "text/plain", headers)

    def _read_json_body(self, limit: int = 1_000_000) -> dict | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self._err(413, "body too large")
            return None
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._err(400, "invalid JSON body")
            return None
        if not isinstance(body, dict):
            self._err(400, "body must be object")
            return None
        return body

    def _handle_login(self) -> None:
        ip = _client_ip(self)
        if not login_allowed(ip):
            self._err(429, "too many login attempts, try later", "rate_limit_error")
            return
        body = self._read_json_body()
        if body is None:
            return
        got = str(body.get("api_key") or body.get("key") or body.get("password") or "").strip()
        if not key_matches(got):
            login_fail(ip)
            log(f"login fail ip={ip}")
            self._err(401, "invalid API key", "authentication_error")
            return
        login_ok(ip)
        token = issue_session()
        log(f"login ok ip={ip}")
        _, payload, ctype = json_bytes({"ok": True})
        self._send(200, payload, ctype, {"Set-Cookie": session_cookie_header(token)})

    def _pool_mutation(self, action: str) -> None:
        if not self._ui_ok():
            self._err(401, "not signed in", "authentication_error")
            return
        if action == "clear":
            result = clear_pool()
        elif action == "import":
            result = import_nine_pool()
        else:
            body = self._read_json_body(limit=8_000_000)
            if body is None:
                return
            if action == "delete":
                result = remove_pool_key(str(body.get("tail") or ""))
            else:
                text = body.get("keys")
                if isinstance(text, list):
                    text = "\n".join(str(x) for x in text)
                result = add_pool_keys(str(text or ""))
        status = 200 if result.get("ok") else 400
        _, payload, ctype = json_bytes(result, status)
        self._send(status, payload, ctype)

    def _proxy_mutation(self, action: str) -> None:
        if not self._ui_ok():
            self._err(401, "not signed in", "authentication_error")
            return
        if action == "import":
            result = import_nine_proxies()
        else:
            body = self._read_json_body(limit=2_000_000)
            if body is None:
                return
            if action == "delete":
                result = remove_proxy(str(body.get("tag") or body.get("url") or ""))
            else:
                text = body.get("proxies")
                if isinstance(text, list):
                    text = "\n".join(str(x) for x in text)
                result = add_proxies(str(text or ""))
        status = 200 if result.get("ok") else 400
        _, payload, ctype = json_bytes(result, status)
        self._send(status, payload, ctype)

    def _handle_logout(self) -> None:
        revoke_session(self)
        extra = {"Set-Cookie": session_cookie_header("deleted", max_age=0)}
        path = self.path.split("?", 1)[0]
        if path == "/logout":
            extra["Location"] = "/login"
            self._send(302, b"", "text/plain", extra)
            return
        _, payload, ctype = json_bytes({"ok": True})
        self._send(200, payload, ctype, extra)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/login", "/login.html"):
            if session_ok(self):
                self._redirect("/")
                return
            self._send_html(WEB_DIR / "login.html")
            return
        if path == "/logout":
            self._handle_logout()
            return
        if path in ("/", "/index.html", "/settings"):
            if not self._ui_ok():
                self._redirect("/login")
                return
            self._send_html(WEB_DIR / "index.html")
            return
        if path in ("/health", "/v1/health"):
            _, payload, ctype = json_bytes(
                {
                    "ok": True,
                    "keys": len(_keys),
                    "upstream": UPSTREAM,
                    "default_model": default_model(),
                    "proxy_enabled": _proxy_enabled,
                    "proxies": len(_proxies),
                }
            )
            self._send(200, payload, ctype)
            return
        if path == "/api/session":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            _, payload, ctype = json_bytes({"ok": True})
            self._send(200, payload, ctype)
            return
        if path == "/api/settings":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            _, payload, ctype = json_bytes(settings_payload())
            self._send(200, payload, ctype)
            return
        if path == "/api/pool":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            _, payload, ctype = json_bytes(pool_view())
            self._send(200, payload, ctype)
            return
        if path == "/api/proxies":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            _, payload, ctype = json_bytes(proxy_view())
            self._send(200, payload, ctype)
            return
        if not self._auth_ok():
            return
        if path in ("/v1/models", "/models"):
            _, payload, ctype = json_bytes({"object": "list", "data": listed_models()})
            self._send(200, payload, ctype)
            return
        self._err(404, f"unknown path {path}")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._handle_logout()
            return
        if path == "/api/settings":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            body = self._read_json_body()
            if body is None:
                return
            _, payload, ctype = json_bytes(save_config_patch(body))
            self._send(200, payload, ctype)
            return
        if path == "/api/probe":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            body = self._read_json_body()
            if body is None:
                return
            mid = str(body.get("model") or "").strip()
            if not mid:
                self._err(400, "model required")
                return
            _, payload, ctype = json_bytes(probe_model(mid))
            self._send(200, payload, ctype)
            return
        if path == "/api/models":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            body = self._read_json_body()
            if body is None:
                return
            _, payload, ctype = json_bytes(add_catalog_model(str(body.get("model") or "")))
            self._send(200, payload, ctype)
            return
        if path == "/api/models/delete":
            if not self._ui_ok():
                self._err(401, "not signed in", "authentication_error")
                return
            body = self._read_json_body()
            if body is None:
                return
            _, payload, ctype = json_bytes(remove_catalog_model(str(body.get("model") or "")))
            self._send(200, payload, ctype)
            return
        if path == "/api/pool":
            self._pool_mutation("add")
            return
        if path == "/api/pool/delete":
            self._pool_mutation("delete")
            return
        if path == "/api/pool/clear":
            self._pool_mutation("clear")
            return
        if path == "/api/pool/import-9router":
            self._pool_mutation("import")
            return
        if path == "/api/proxies":
            self._proxy_mutation("add")
            return
        if path == "/api/proxies/delete":
            self._proxy_mutation("delete")
            return
        if path == "/api/proxies/import-9router":
            self._proxy_mutation("import")
            return
        if not self._auth_ok():
            return
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._err(404, f"unknown path {path}")
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._err(400, "invalid JSON body")
            return
        if not isinstance(body, dict):
            self._err(400, "body must be object")
            return
        req_body = prepare_request_body(body)
        stream = bool(req_body.get("stream"))
        model = req_body.get("model")
        eff = req_body.get("reasoning_effort")
        if model not in _public_models:
            log(f"reject unlisted model={model}")
            self._err(404, f"model not in catalog: {model}")
            return
        log(f"POST chat model={model} stream={stream} effort={eff} msgs={len(req_body.get('messages') or [])}")
        if stream:
            self._proxy_stream(req_body)
        else:
            self._proxy_json(req_body)

    def _proxy_json(self, req_body: dict) -> None:
        data = json.dumps(req_body).encode("utf-8")
        eff = str(req_body.get("reasoning_effort") or "")
        last_err = "own key pool is empty — add sk_ keys in the dashboard" if not _keys else "upstream failed"
        last_status = 503 if not _keys else 502
        last_body = b""
        served_key = ""
        served_proxy = ""
        for _attempt in range(MAX_FAILOVER):
            key = next_key()
            if not key:
                break
            px = next_proxy()
            via = proxy_tag(px) if px else "direct"
            log(f"try json key={key_tag(key)} via {via}")
            try:
                req = Request(UPSTREAM, data=data, headers=cline_headers(key), method="POST")
                with open_upstream(req, timeout=180, proxy_url=px) as resp:
                    raw = resp.read()
                    status = resp.status
            except HTTPError as e:
                raw = e.read() or b""
                status = e.code
                last_status, last_body, last_err = status, raw, f"HTTP {status}"
                served_key, served_proxy = key_tag(key), via.strip()
                if status in RETRY_STATUSES:
                    log(f"failover json key={key_tag(key)} via {via} status={status} body={raw[:180]!r}")
                    continue
                log(f"fail json key={key_tag(key)} via {via} status={status}")
                record_hit(str(req_body.get("model") or ""), stream=False, ok=False, effort=eff, key_tail=key_tag(key), proxy=via, key_n=key_index(key), proxy_n=proxy_index(px))
                self._send(status, raw, "application/json")
                return
            except (URLError, TimeoutError, OSError) as e:
                last_status, last_err = 502, str(e)
                served_key, served_proxy = key_tag(key), via.strip()
                log(f"failover json key={key_tag(key)} via {via} net={e}")
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                self._send(status, raw, "application/json")
                return
            obj = normalize_completion(obj)
            log(f"ok json key={key_tag(key)} via {via} status={status}")
            record_hit(str(req_body.get("model") or ""), stream=False, ok=True, usage_obj=obj, effort=eff, key_tail=key_tag(key), proxy=via, key_n=key_index(key), proxy_n=proxy_index(px))
            _, payload, ctype = json_bytes(obj)
            self._send(200, payload, ctype)
            return
        record_hit(str(req_body.get("model") or ""), stream=False, ok=False, effort=eff, key_tail=served_key, proxy=served_proxy)
        _, payload, ctype = json_bytes(
            {"error": {"message": last_err, "type": "api_error", "code": last_status, "body": last_body[:300].decode("utf-8", "replace")}}
        )
        self._send(last_status if last_status >= 400 else 502, payload, ctype)

    def _proxy_stream(self, req_body: dict) -> None:
        data = json.dumps(req_body).encode("utf-8")
        eff = str(req_body.get("reasoning_effort") or "")
        last_err = "own key pool is empty — add sk_ keys in the dashboard" if not _keys else "upstream failed"
        last_status = 503 if not _keys else 502
        served_key = ""
        served_proxy = ""
        for _attempt in range(MAX_FAILOVER):
            key = next_key()
            if not key:
                break
            px = next_proxy()
            via = proxy_tag(px) if px else "direct"
            log(f"try stream key={key_tag(key)} via {via}")
            try:
                req = Request(UPSTREAM, data=data, headers=cline_headers(key), method="POST")
                resp = open_upstream(req, timeout=180, proxy_url=px)
            except HTTPError as e:
                raw = e.read() or b""
                last_status, last_err = e.code, f"HTTP {e.code} {raw[:180]!r}"
                served_key, served_proxy = key_tag(key), via.strip()
                if e.code in RETRY_STATUSES:
                    log(f"failover stream key={key_tag(key)} via {via} status={e.code}")
                    continue
                log(f"fail stream key={key_tag(key)} via {via} status={e.code}")
                record_hit(str(req_body.get("model") or ""), stream=True, ok=False, effort=eff, key_tail=key_tag(key), proxy=via, key_n=key_index(key), proxy_n=proxy_index(px))
                self._send(e.code, raw, e.headers.get("Content-Type") or "application/json")
                return
            except (URLError, TimeoutError, OSError) as e:
                last_status, last_err = 502, str(e)
                served_key, served_proxy = key_tag(key), via.strip()
                log(f"failover stream key={key_tag(key)} via {via} net={e}")
                continue
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in ctype and "application/json" in ctype:
                # upstream didn't stream — convert to SSE
                raw = resp.read()
                try:
                    obj = normalize_completion(json.loads(raw.decode("utf-8", "replace")))
                except Exception:
                    record_hit(str(req_body.get("model") or ""), stream=True, ok=True, effort=eff, key_tail=key_tag(key), proxy=via, key_n=key_index(key), proxy_n=proxy_index(px))
                    self._send(200, raw, "application/json")
                    return
                self._sse_from_json(obj, effort=eff, key_tail=key_tag(key), proxy=via, key_n=key_index(key), proxy_n=proxy_index(px))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            buf = b""
            rc_chars = 0
            last_usage: dict[str, Any] | None = None
            aborted = False
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        parsed = _sse_usage(line)
                        if parsed is not None:
                            last_usage = parsed
                        out = self._map_sse_line(line)
                        if out is None:
                            continue
                        if b"reasoning_content" in out:
                            rc_chars += 1
                        self.wfile.write(out + b"\n")
                        self.wfile.flush()
                if buf.strip():
                    parsed = _sse_usage(buf)
                    if parsed is not None:
                        last_usage = parsed
                    out = self._map_sse_line(buf)
                    if out is not None:
                        self.wfile.write(out + b"\n")
                log(f"stream done rc_events~={rc_chars}")
            except Exception as e:
                aborted = True
                log(f"stream abort: {e}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            record_hit(
                str(req_body.get("model") or ""),
                stream=True,
                ok=not aborted,
                usage_obj=last_usage,
                effort=eff,
                key_tail=key_tag(key),
                proxy=via,
                key_n=key_index(key),
                proxy_n=proxy_index(px),
            )
            return
        record_hit(str(req_body.get("model") or ""), stream=True, ok=False, effort=eff, key_tail=served_key, proxy=served_proxy)
        _, payload, ctype = json_bytes(
            {"error": {"message": last_err, "type": "api_error", "code": last_status}}
        )
        self._send(last_status if last_status >= 400 else 502, payload, ctype)

    def _sse_from_json(self, obj: dict, effort: str = "", key_tail: str = "", proxy: str = "", key_n: int = 0, proxy_n: int = 0) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        created = int(obj.get("created") or time.time())
        mid = obj.get("id") or f"chatcmpl-{created}"
        model = obj.get("model") or default_model()
        ch0 = (obj.get("choices") or [{}])[0]
        msg = ch0.get("message") or {}
        role = msg.get("role") or "assistant"
        rc = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        finish = ch0.get("finish_reason") or "stop"

        def emit(delta: dict, finish_reason=None):
            payload = {
                "id": mid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        emit({"role": role})
        if rc:
            emit({"reasoning": rc, "reasoning_content": rc})
        if content:
            emit({"content": content})
        emit({}, finish)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        record_hit(str(obj.get("model") or ""), stream=True, ok=True, usage_obj=obj, effort=effort, key_tail=key_tail, proxy=proxy, key_n=key_n, proxy_n=proxy_n)

    def _map_sse_line(self, line: bytes) -> bytes | None:
        raw = line.rstrip(b"\r")
        if not raw:
            return b""
        if not raw.startswith(b"data:"):
            return raw
        payload = raw[5:].strip()
        if payload == b"[DONE]":
            return b"data: [DONE]\n"
        if not payload:
            return raw
        try:
            obj = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            return raw
        obj = unwrap_body(obj)
        if isinstance(obj, dict):
            inject_reasoning_fields(obj)
            if "object" not in obj:
                obj["object"] = "chat.completion.chunk"
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"


def main() -> None:
    global _nine_keys, _nine_proxies, _proxy_key, _default_effort, HOST, PORT, _proxy_enabled, _proxies, _model_efforts, _public_models
    ROOT.mkdir(parents=True, exist_ok=True)
    cfg = load_or_create_config()
    _proxy_key = cfg["api_key"]
    _default_effort = _norm_effort(cfg.get("reasoning_effort")) or "max"
    seen: set[str] = set()
    loaded: list[str] = []
    for raw in cfg.get("public_models") or BUILTIN_MODELS:
        mid = resolve_model(str(raw).strip()) if str(raw).strip() else ""
        if mid and mid not in seen:
            seen.add(mid)
            loaded.append(mid)
    _public_models = loaded or list(BUILTIN_MODELS)
    _model_efforts = {}
    for m, eff in (cfg.get("model_reasoning_effort") or {}).items():
        ne = _norm_effort(eff)
        if ne and ne in VALID_EFFORTS:
            _model_efforts[resolve_model(m)] = ne
    _proxy_enabled = bool(cfg.get("proxy_enabled"))
    try:
        own = load_own_keys()
    except Exception as e:
        raise SystemExit(f"bad keys file {KEYS_FILE}: {e}") from e
    _apply_own_keys(own)
    _nine_keys = load_nine_keys()
    try:
        own_px = load_own_proxies()
    except Exception as e:
        raise SystemExit(f"bad proxies file {PROXIES_FILE}: {e}") from e
    _apply_proxies(own_px)
    _nine_proxies = load_nine_proxies()
    host = cfg.get("host") or HOST
    port = int(cfg.get("port") or PORT)
    HOST, PORT = host, port
    init_stats_db()
    threading.Thread(target=stats_writer_loop, name="stats-writer", daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    log(f"cline-reason-proxy listening http://{host}:{port}/")
    log(f"settings UI: http://127.0.0.1:{port}/")
    log(f"Cline keys loaded: {len(_keys)} from {KEYS_FILE.name} (RR consume-1, failover={MAX_FAILOVER})")
    if not _keys:
        log("own key pool is empty — /v1 chat returns 503 until keys are added in the dashboard")
    log(f"RR head: {', '.join(key_tag(k) for k in _keys[:8])}")
    log(f"9router import available: {len(_nine_keys)} (not in the live pool)")
    log(f"egress proxies: {len(_proxies)} enabled={_proxy_enabled}")
    if _proxies:
        log(f"proxy head: {', '.join(proxy_tag(u) for u in _proxies[:8])}")
    log(f"Auth: Authorization: Bearer <api_key in {CONFIG_PATH}>")
    log(f"Default model: {default_model()}")
    log(f"reasoning effort default: {_default_effort}")
    log("include_reasoning=true injected; reasoning -> reasoning_content")
    threading.Thread(target=watchdog_loop, name="sd-watchdog", daemon=True).start()
    sd_notify("READY=1")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stop")


if __name__ == "__main__":
    main()
