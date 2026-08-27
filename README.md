# cline-reason-proxy

OpenAI-compatible local proxy in front of Cline (`api.cline.bot`).

Cline VS Code streams `delta.reasoning` + `include_reasoning`. Hermes / OpenAI clients expect `delta.reasoning_content`. This proxy:

- injects `include_reasoning: true`
- copies `reasoning` / `reasoning_details` → `reasoning_content`
- unwraps `{data, success}` envelopes
- round-robins Cline `sk_` keys
- serves a settings UI at `/` (API key, reasoning effort, models)

Default model: `z-ai/glm-5.3-flash` (free Cline flash). Old picker names still remap.

## Windows

```
python proxy.py
```

Settings: http://127.0.0.1:20129/

OpenAI:

```
base_url: http://127.0.0.1:20129/v1
Authorization: Bearer <api_key from config.json>
model: z-ai/glm-5.3-flash
```

Upstream keys: active `provider=cline` rows in `%APPDATA%\9router\db\data.sqlite`.

WSL2 cannot use `127.0.0.1` (that is the VM). Use the Windows vEthernet IP:

```
ip route | awk '/^default/{print $3}'
# then http://<that-ip>:20129/v1
```

## Ubuntu / Linux / WSL (native)

```bash
sudo apt update && sudo apt install -y python3
git clone https://github.com/kevinnft/cline-reason-proxy.git
cd cline-reason-proxy
chmod +x run.sh
```

Cline keys (pick one):

1. `cp keys.example.json keys.json` and put `sk_…` values
2. `export CLINE_API_KEYS='sk_aaa,sk_bbb'`
3. 9router sqlite at `~/.9router/db/data.sqlite` or `NINE_ROUTER_DB=/path/to/data.sqlite`

```bash
./run.sh
```

Then open http://127.0.0.1:20129/

Do **not** prefix Cline API keys with `workos:` — that 401s (`sk_` keys are Bearer as-is).

## Files that must stay local

`config.json` and `keys.json` are gitignored. Copy the `*.example.json` files.

## Auth split

| Hop | Key |
|---|---|
| Client → proxy | one `api_key` in `config.json` |
| Proxy → Cline | pool of `sk_` keys (round-robin + failover on 401/402/429/5xx, up to 8) |
