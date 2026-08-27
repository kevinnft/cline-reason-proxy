#!/usr/bin/env python3
"""OpenAI-compatible Cline proxy with API-key auth + reasoning remap.

Cline VSCode reads delta.reasoning / include_reasoning.
Hermes / OpenAI clients read delta.reasoning_content.
This proxy:
  - requires Bearer PROXY_API_KEY
  - round-robins Cline keys from 9router sqlite
  - injects include_reasoning=true
  - copies reasoning / reasoning_details -> reasoning_content
  - unwraps {data, success} envelopes
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

# Old picker names still remap; /v1/models lists PUBLIC_MODELS only.
MODEL_ALIASES = {
    "cline-glm-5.3-flash": "z-ai/glm-5.3-flash",
    "cline/z-ai/glm-5.3-flash": "z-ai/glm-5.3-flash",
    "glm-5.3-flash": "z-ai/glm-5.3-flash",
}

DEFAULT_MODEL = "z-ai/glm-5.3-flash"
PUBLIC_MODELS = [DEFAULT_MODEL]
RETRY_STATUSES = {401, 402, 403, 408, 409, 429, 500, 502, 503, 504}
MAX_FAILOVER = 8

_lock = threading.Lock()
_rr = 0
_keys: list[str] = []
_proxy_key = ""
_default_effort = "max"
WEB_DIR = ROOT / "web"
PAID_MODELS: set[str] = set()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return cfg


def load_cline_keys() -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def add(k: str) -> None:
        k = (k or "").strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)

    env_keys = os.environ.get("CLINE_API_KEYS") or os.environ.get("CLINE_API_KEY") or ""
    for part in env_keys.replace(";", ",").split(","):
        add(part)

    if KEYS_FILE.is_file():
        try:
            blob = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            raise SystemExit(f"bad keys file {KEYS_FILE}: {e}") from e
        if isinstance(blob, list):
            for item in blob:
                if isinstance(item, str):
                    add(item)
                elif isinstance(item, dict):
                    add(str(item.get("apiKey") or item.get("key") or ""))
        elif isinstance(blob, dict):
            for item in blob.get("keys") or blob.get("apiKeys") or []:
                add(item if isinstance(item, str) else str((item or {}).get("apiKey") or ""))

    db = default_nine_db()
    if db.is_file():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT data FROM providerConnections WHERE provider='cline' AND isActive=1"
        ).fetchall()
        con.close()
        for (blob,) in rows:
            try:
                d = json.loads(blob)
            except Exception:
                continue
            add(d.get("apiKey") or "")

    if not keys:
        raise SystemExit(
            "no Cline keys. Put sk_ keys in keys.json, set CLINE_API_KEYS, "
            f"or point NINE_ROUTER_DB at 9router sqlite (tried {db})"
        )
    return keys


def next_keys(n: int) -> list[str]:
    global _rr
    with _lock:
        if not _keys:
            return []
        out = []
        start = _rr
        for i in range(min(n, len(_keys))):
            out.append(_keys[(start + i) % len(_keys)])
        _rr = (start + 1) % len(_keys)
        return out


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


def resolve_model(name: str | None) -> str:
    if not name:
        return DEFAULT_MODEL
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


EFFORT_MAP = {
    "xhigh": "max",
    "extra-high": "max",
    "extra_high": "max",
    "ultra": "max",
}


def _norm_effort(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    v = val.strip().lower()
    return EFFORT_MAP.get(v, v)


def prepare_request_body(body: dict) -> dict:
    body = dict(body)
    body["model"] = resolve_model(body.get("model"))
    if "include_reasoning" not in body:
        body["include_reasoning"] = True
    # Cline VSCode sets this; keep if client sent, else enable
    if body.get("stream") and "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    # GLM-5.3-flash Cline catalog: effort values low|high|max. xhigh is Hermes-speak → max.
    effort = None
    r = body.get("reasoning")
    if isinstance(r, dict) and r.get("effort"):
        effort = _norm_effort(r.get("effort"))
    if effort is None:
        effort = _norm_effort(body.get("reasoning_effort"))
    if not effort:
        effort = _default_effort or "max"
    body["reasoning"] = {**(r if isinstance(r, dict) else {}), "effort": effort}
    body["reasoning_effort"] = effort
    return body


def listed_models() -> list[dict]:
    out = []
    seen = set()
    for mid in PUBLIC_MODELS:
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


def settings_payload() -> dict:
    models = listed_models()
    return {
        "ok": True,
        "api_key": _proxy_key,
        "host": HOST,
        "port": PORT,
        "keys": len(_keys),
        "reasoning_effort": _default_effort,
        "include_reasoning": True,
        "default_model": DEFAULT_MODEL,
        "models": models,
        "upstream": UPSTREAM,
    }


def save_config_patch(patch: dict) -> dict:
    global _default_effort, _proxy_key
    cfg = load_or_create_config()
    if "reasoning_effort" in patch:
        effort = _norm_effort(patch.get("reasoning_effort")) or "max"
        if effort not in ("low", "high", "max"):
            effort = "max"
        cfg["reasoning_effort"] = effort
        _default_effort = effort
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return settings_payload()


def extract_bearer(handler: BaseHTTPRequestHandler) -> str:
    auth = handler.headers.get("Authorization") or handler.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (handler.headers.get("x-api-key") or "").strip()


def json_bytes(obj: Any, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
        if not got or not secrets.compare_digest(got, _proxy_key):
            self._err(401, "API key required. Set Authorization: Bearer <proxy api key>", "authentication_error")
            return False
        return True

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/settings"):
            html = WEB_DIR / "index.html"
            if not html.is_file():
                self._err(404, "web/index.html missing")
                return
            self._send(200, html.read_bytes(), "text/html; charset=utf-8")
            return
        if path in ("/health", "/v1/health"):
            _, payload, ctype = json_bytes(
                {"ok": True, "keys": len(_keys), "upstream": UPSTREAM, "default_model": DEFAULT_MODEL}
            )
            self._send(200, payload, ctype)
            return
        if path == "/api/settings":
            _, payload, ctype = json_bytes(settings_payload())
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
        if path == "/api/settings":
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
            _, payload, ctype = json_bytes(save_config_patch(body))
            self._send(200, payload, ctype)
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
        log(f"POST chat model={model} stream={stream} msgs={len(req_body.get('messages') or [])}")
        if stream:
            self._proxy_stream(req_body)
        else:
            self._proxy_json(req_body)

    def _proxy_json(self, req_body: dict) -> None:
        data = json.dumps(req_body).encode("utf-8")
        last_err = "upstream failed"
        last_status = 502
        last_body = b""
        for key in next_keys(MAX_FAILOVER):
            try:
                req = Request(UPSTREAM, data=data, headers=cline_headers(key), method="POST")
                with urlopen(req, timeout=180) as resp:
                    raw = resp.read()
                    status = resp.status
            except HTTPError as e:
                raw = e.read() or b""
                status = e.code
                last_status, last_body, last_err = status, raw, f"HTTP {status}"
                if status in RETRY_STATUSES:
                    log(f"failover json status={status} body={raw[:180]!r}")
                    continue
                self._send(status, raw, "application/json")
                return
            except (URLError, TimeoutError, OSError) as e:
                last_status, last_err = 502, str(e)
                log(f"failover json net={e}")
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                self._send(status, raw, "application/json")
                return
            obj = normalize_completion(obj)
            _, payload, ctype = json_bytes(obj)
            self._send(200, payload, ctype)
            return
        _, payload, ctype = json_bytes(
            {"error": {"message": last_err, "type": "api_error", "code": last_status, "body": last_body[:300].decode("utf-8", "replace")}}
        )
        self._send(last_status if last_status >= 400 else 502, payload, ctype)

    def _proxy_stream(self, req_body: dict) -> None:
        data = json.dumps(req_body).encode("utf-8")
        last_err = "upstream failed"
        last_status = 502
        for key in next_keys(MAX_FAILOVER):
            try:
                req = Request(UPSTREAM, data=data, headers=cline_headers(key), method="POST")
                resp = urlopen(req, timeout=180)
            except HTTPError as e:
                raw = e.read() or b""
                last_status, last_err = e.code, f"HTTP {e.code} {raw[:180]!r}"
                if e.code in RETRY_STATUSES:
                    log(f"failover stream status={e.code}")
                    continue
                self._send(e.code, raw, e.headers.get("Content-Type") or "application/json")
                return
            except (URLError, TimeoutError, OSError) as e:
                last_status, last_err = 502, str(e)
                log(f"failover stream net={e}")
                continue
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in ctype and "application/json" in ctype:
                # upstream didn't stream — convert to SSE
                raw = resp.read()
                try:
                    obj = normalize_completion(json.loads(raw.decode("utf-8", "replace")))
                except Exception:
                    self._send(200, raw, "application/json")
                    return
                self._sse_from_json(obj)
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
            c_chars = 0
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        out = self._map_sse_line(line)
                        if out is None:
                            continue
                        if b"reasoning_content" in out:
                            rc_chars += 1
                        self.wfile.write(out + b"\n")
                        self.wfile.flush()
                if buf.strip():
                    out = self._map_sse_line(buf)
                    if out is not None:
                        self.wfile.write(out + b"\n")
                log(f"stream done rc_events~={rc_chars}")
            except Exception as e:
                log(f"stream abort: {e}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            return
        _, payload, ctype = json_bytes(
            {"error": {"message": last_err, "type": "api_error", "code": last_status}}
        )
        self._send(last_status if last_status >= 400 else 502, payload, ctype)

    def _sse_from_json(self, obj: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        created = int(obj.get("created") or time.time())
        mid = obj.get("id") or f"chatcmpl-{created}"
        model = obj.get("model") or DEFAULT_MODEL
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
    global _keys, _proxy_key, _default_effort, HOST, PORT
    ROOT.mkdir(parents=True, exist_ok=True)
    cfg = load_or_create_config()
    _proxy_key = cfg["api_key"]
    _default_effort = _norm_effort(cfg.get("reasoning_effort")) or "max"
    _keys = load_cline_keys()
    host = cfg.get("host") or HOST
    port = int(cfg.get("port") or PORT)
    HOST, PORT = host, port
    httpd = ThreadingHTTPServer((host, port), Handler)
    log(f"cline-reason-proxy listening http://{host}:{port}/")
    log(f"settings UI: http://127.0.0.1:{port}/")
    log(f"Cline keys loaded: {len(_keys)}")
    log(f"Auth: Authorization: Bearer <api_key in {CONFIG_PATH}>")
    log(f"Default model: {DEFAULT_MODEL}")
    log(f"reasoning effort default: {_default_effort}")
    log("include_reasoning=true injected; reasoning -> reasoning_content")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stop")


if __name__ == "__main__":
    main()
