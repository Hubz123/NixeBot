
# --- bootstrap path for local runs ---
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.abspath(__file__))
_PROJ = _os.path.abspath(_os.path.join(_ROOT, ".."))
if _PROJ not in _sys.path:
    _sys.path.insert(0, _PROJ)
# -------------------------------------


import argparse, os, sys, asyncio, logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smoke_runtime_providers_v2")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--img", help="Path to image", required=False)
    args = parser.parse_args()

    try:
        from nixe.cogs import lucky_pull_auto as _lpa
    except Exception as e:
        log.error("import lucky_pull_auto failed: %r", e)
        sys.exit(1)

    class Dummy:
        timeout_ms = 20000

    dummy = Dummy()
    classify = getattr(_lpa.LuckyPullAuto, "_classify", None)
    if not classify:
        log.error("No classifier available (overlay not applied).")
        sys.exit(2)

    img_bytes = None
    if args.img:
        with open(args.img, "rb") as f:
            img_bytes = f.read()

    prob, via = await classify(dummy, img_bytes, None)  # type: ignore
    print(f"[RESULT] ok={prob>=0.85} score={prob:.3f} via={via}")
    print("== SUMMARY == PASS (runtime classifier active)")

if __name__ == "__main__":
    asyncio.run(main())
