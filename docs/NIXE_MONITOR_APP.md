# Nixe Monitor App (PC + Render)

1. Install deps: `pip install psutil`
2. Run agent: `python nixe_telemetry_agent.py`
3. Open dashboard: `nixe-monitor.html`

Render metrics requires `/metrics.json` on the Render web app; otherwise dashboard falls back to `/healthz`.

Optional:
- Force match process: `set BOT_MATCH=main.py`
- Expose agent via Tailscale: set `TELEMETRY_BIND=0.0.0.0`.
