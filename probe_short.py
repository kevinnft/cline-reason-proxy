import json, time, urllib.request, urllib.error
from pathlib import Path

cfg = json.loads(Path(r"C:/Users/ASUS/AppData/Local/cline-reason-proxy/config.json").read_text(encoding="utf-8"))
KEY = cfg["api_key"]
BASE = "http://127.0.0.1:20129"

def req(path, method="GET", body=None, auth=True, timeout=90):
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

print("=== health ===")
st, h, raw, dt = req("/health", auth=False, timeout=5)
print(st, raw.decode()[:200], round(dt, 2))

print("=== NONSTREAM ===")
st, h, raw, dt = req("/v1/chat/completions", "POST", {
    "model": "cline-glm-5.3-flash",
    "messages": [{"role": "user", "content": "Think then answer only the number: 17*19?"}],
    "stream": False,
    "max_tokens": 200,
}, timeout=90)
print("status", st, "dt", round(dt, 2), "bytes", len(raw))
text = raw.decode("utf-8", "replace")
print("head", text[:500])
try:
    obj = json.loads(text)
except Exception as e:
    print("json fail", e)
    obj = {}
if "error" in obj:
    print("ERROR", obj["error"])
else:
    ch = (obj.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    print("msg_keys", list(msg.keys()))
    rc = msg.get("reasoning_content")
    rs = msg.get("reasoning")
    print("rc_len", len(rc) if isinstance(rc, str) else rc)
    print("rs_len", len(rs) if isinstance(rs, str) else type(rs))
    print("content", (msg.get("content") or "")[:180])
    print("envelope", "success" in obj, "data" in obj)
