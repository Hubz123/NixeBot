
# SMOKE_LPG_GUARD_ONLINE — End-to-end Lucky Pull

This script simulates the full path you described:

**smoke → Gemini detect → (if lucky) delete message + send persona + mention + redirect channel**

### Quick start (Windows)

```bat
python scripts\smoke_lpg_guard_online.py ^
  --img C:\Users\CC-PC\Pictures\Camera Roll\image.png ^
  --chan-id 886534544688308265 ^
  --thread-id 1429675223761948853 ^
  --user-id 123456789012345678 ^
  --redirect 1293200121063936052 ^
  --persona-file nixe\config\yandere.json ^
  --persona-random ^
  --ttl 5 ^
  --dotenv .env ^
  --runtime-json nixe\config\runtime_env.json
```

- Requires **DISCORD_TOKEN** and **GEMINI_API_KEY** (optionally **GEMINI_API_KEY_B**) in `.env`
- Uses your `GEMINI_MODEL` from env if set; default `gemini-2.5-flash-lite`
- Threshold from `LPG_THRESHOLD` env (default **0.85**)

### Notes

- The **image post** is done first to the given `--thread-id` (or `--chan-id`).
- Gemini classifies the local file (no upload to Discord needed for classification itself).
- If **lucky** and `score >= threshold`:
  - Deletes the image message after `--ttl` seconds (0 = immediate),
  - Sends a persona line (from your `yandere.json`) + **@mention** (if `--user-id` given) + redirect to `<#redirect>`.
- If **not lucky**, the image stays and a status line is posted.
