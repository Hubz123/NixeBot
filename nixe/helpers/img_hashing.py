import io
from typing import List

try:
    from PIL import Image, ImageSequence
except Exception:
    Image = None
    ImageSequence = None

try:
    import imagehash
except Exception:
    imagehash = None


def _iter_frames(im, max_frames: int):
    if getattr(im, "is_animated", False) and ImageSequence is not None:
        it = ImageSequence.Iterator(im)
    else:
        it = [im]
    c = 0
    for fr in it:
        yield fr
        c += 1
        if c >= max_frames:
            break


def dhash_list_from_bytes(data: bytes, max_frames: int = 6) -> List[str]:
    out: List[str] = []
    if not data or not Image:
        return out

    bio = io.BytesIO(data)
    try:
        with Image.open(bio) as im:
            seen = set()
            for fr in _iter_frames(im, max_frames=max_frames):
                # dHash: compare adjacent pixels horizontally on an 9x8 grayscale image.
                g = fr.convert("L").resize((9, 8))
                try:
                    px = list(g.getdata())
                    w, h = g.size
                    bits = []
                    for y in range(h):
                        row = px[y * w : (y + 1) * w]
                        for x in range(w - 1):
                            bits.append(1 if row[x] > row[x + 1] else 0)
                    # pack bits into hex string
                    v = 0
                    for b in bits:
                        v = (v << 1) | b
                    hx = f"{v:016x}"
                    if hx not in seen:
                        seen.add(hx)
                        out.append(hx)
                finally:
                    try:
                        g.close()
                    except Exception:
                        pass
    except Exception:
        return out
    finally:
        try:
            bio.close()
        except Exception:
            pass
    return out


def phash_list_from_bytes(data: bytes, max_frames: int = 6) -> List[str]:
    out: List[str] = []
    if not data or not Image or not imagehash:
        return out

    bio = io.BytesIO(data)
    try:
        with Image.open(bio) as im:
            seen = set()
            for fr in _iter_frames(im, max_frames=max_frames):
                fr2 = None
                try:
                    fr2 = fr.copy()
                    h = str(imagehash.phash(fr2.convert("RGB")))
                    if h not in seen:
                        seen.add(h)
                        out.append(h)
                finally:
                    if fr2 is not None:
                        try:
                            fr2.close()
                        except Exception:
                            pass
    except Exception:
        return out
    finally:
        try:
            bio.close()
        except Exception:
            pass
    return out
