import json, time, urllib.request, urllib.error
from pathlib import Path

cfg = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
KEY = cfg["api_key"]
BASE = "http://127.0.0.1:20129"

def req(path, method="GET", body=None, auth=True, timeout=180):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + KEY
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers.items()), raw, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b"", time.time() - t0

print("=== health (no auth) ===")
st, h, raw, dt = req("/health", auth=False)
print(st, raw.decode()[:300], "dt", round(dt, 2))

print("\n=== models no auth ===")
st, h, raw, dt = req("/v1/models", auth=False)
print(st, raw.decode()[:200])

print("\n=== models with auth ===")
st, h, raw, dt = req("/v1/models", auth=True)
print(st, raw.decode()[:400])

prompt = "Think step by step then answer with only the number: 17*19?"

print("\n=== NONSTREAM cline-glm-5.3-flash ===")
st, h, raw, dt = req("/v1/chat/completions", "POST", {
    "model": "cline-glm-5.3-flash",
    "messages": [{"role": "user", "content": prompt}],
    "stream": False,
    "max_tokens": 400,
})
print("status", st, "dt", round(dt, 2), "bytes", len(raw), "ct", h.get("Content-Type"))
text = raw.decode("utf-8", "replace")
print("head", text[:600])
try:
    obj = json.loads(text)
except Exception as e:
    print("json fail", e)
    obj = {}
print("top_keys", list(obj.keys()) if isinstance(obj, dict) else type(obj))
ch = (obj.get("choices") or [{}])[0] if isinstance(obj, dict) else {}
msg = ch.get("message") or {}
print("message_keys", list(msg.keys()))
rc = msg.get("reasoning_content")
rs = msg.get("reasoning")
print("reasoning_content_len", len(rc) if isinstance(rc, str) else rc)
print("reasoning_len", len(rs) if isinstance(rs, str) else type(rs))
print("content", (msg.get("content") or "")[:200])
print("usage", obj.get("usage") if isinstance(obj, dict) else None)
print("envelope?", "success" in obj, "data" in obj)

print("\n=== STREAM cline-glm-5.3-flash ===")
st, h, raw, dt = req("/v1/chat/completions", "POST", {
    "model": "cline-glm-5.3-flash",
    "messages": [{"role": "user", "content": prompt}],
    "stream": True,
    "max_tokens": 400,
})
print("status", st, "dt", round(dt, 2), "bytes", len(raw), "ct", h.get("Content-Type"))
text = raw.decode("utf-8", "replace")
print("has_reasoning_content", "reasoning_content" in text)
print("has_reasoning_field", '"reasoning"' in text)
print("has_data_success_envelope", '"success": true' in text or '"success":true' in text)
rc_len = 0
c_len = 0
n = 0
keys = set()
for line in text.splitlines():
    if not line.startswith("data:"):
        continue
    p = line[5:].strip()
    if not p or p == "[DONE]":
        continue
    n += 1
    try:
        o = json.loads(p)
    except Exception:
        continue
    d = ((o.get("choices") or [{}])[0].get("delta")) or {}
    keys |= set(d.keys())
    if isinstance(d.get("reasoning_content"), str):
        rc_len += len(d["reasoning_content"])
    if isinstance(d.get("reasoning"), str) and not isinstance(d.get("reasoning_content"), str):
        pass
    if isinstance(d.get("content"), str):
        c_len += len(d["content"])
print("sse_chunks", n, "delta_keys", keys, "rc_len", rc_len, "content_len", c_len)
print("sse_head", text[:400].replace("\n", " | "))
