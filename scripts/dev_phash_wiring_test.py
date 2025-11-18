import asyncio
from pathlib import Path

# Import helper & env from LPG thread bridge
from nixe.cogs.a00_lpg_thread_bridge_guard import _phash64_bytes, Image, np


class DummyAttachment:
    def __init__(self, path: Path):
        self.filename = path.name
        # Content-type kira-kira saja, cukup untuk test
        self.content_type = "image/png"
        self._data = path.read_bytes()

    async def read(self):
        return self._data


class DummyChannel:
    def __init__(self, cid: int = 123):
        self.id = cid


class DummyMessage:
    def __init__(self, path: Path):
        self.id = 999_999_999_999
        self.channel = DummyChannel()
        self.attachments = [DummyAttachment(path)]


async def main(img_path: str):
    p = Path(img_path)
    if not p.is_file():
        raise SystemExit(f"File not found: {p}")

    print("=== ENV CHECK ===")
    print("Pillow Image is None? ", Image is None)
    print("NumPy is None?       ", np is None)

    msg = DummyMessage(p)

    # Meniru wiring di on_message: simpan bytes + pHash ke __dict__
    raw_bytes = await msg.attachments[0].read()
    d = msg.__dict__
    d["_nixe_imgbytes"] = raw_bytes
    d["_nixe_phash"] = _phash64_bytes(raw_bytes) if raw_bytes else None

    print("\n=== RESULT ===")
    ph = d.get("_nixe_phash")
    print("type(_nixe_phash) =", type(ph))
    print("_nixe_phash (int) =", ph)
    if isinstance(ph, int):
        print("_nixe_phash (hex) =", f"{ph:016X}")
    else:
        print("pHash FAILED (None / bukan int)")


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m scripts.dev_phash_wiring_test path/to/image.png")
        raise SystemExit(1)

    asyncio.run(main(sys.argv[1]))
