"""Perceptual hash of a comic's cover, compatible with Metron's ``cover_hash``.

Metron stores ``imagehash.phash()`` of every issue's main cover and can search
it by Hamming distance (``/api/issue/?cover_hash_near=``). That lets a file be
matched to an issue from what is inside it rather than from its name.

The hash is reimplemented here on Pillow alone. ``imagehash`` would pull numpy,
scipy and PyWavelets into the image for one 32x32 transform. The algorithm is
small and fixed: greyscale, Lanczos to 32x32, 2-D DCT-II, keep the 8x8
low-frequency corner, one bit per coefficient above the median. Rounding can
differ from scipy's by a bit on a coefficient that sits on the median, which a
distance search absorbs. ``tests/unit/test_cover_hash.py`` pins it against
vectors produced by the real library.
"""

import math
import os

from core.app_logging import app_logger

HASH_SIZE = 8
_SAMPLE = HASH_SIZE * 4  # imagehash's highfreq_factor

# A single cover is about 0.65 wide per unit of height and a wraparound spread
# about 1.3. Metron's own guidance for clients is to split above 1.2.
SPREAD_RATIO = 1.2

# cos(pi * k * (2n + 1) / 2N) for the 8 frequencies kept. The DCT's scale
# factor is dropped: only the order of the coefficients matters.
_COS = [
    [math.cos(math.pi * k * (2 * n + 1) / (2 * _SAMPLE)) for n in range(_SAMPLE)]
    for k in range(HASH_SIZE)
]


def phash_hex(img) -> str:
    """``str(imagehash.phash(img))``: 16 hex characters."""
    from PIL import Image

    small = img.convert("L").resize((_SAMPLE, _SAMPLE), Image.Resampling.LANCZOS)
    flat = list(small.getdata())
    rows = [flat[y * _SAMPLE:(y + 1) * _SAMPLE] for y in range(_SAMPLE)]

    # Along x first: one 8-wide row per pixel row. Then along y.
    partial = [
        [sum(p * c for p, c in zip(row, cos_v)) for cos_v in _COS]
        for row in rows
    ]
    coeffs = [
        sum(partial[y][v] * cos_u[y] for y in range(_SAMPLE))
        for cos_u in _COS
        for v in range(HASH_SIZE)
    ]

    ordered = sorted(coeffs)
    half = len(ordered) // 2
    median = (ordered[half - 1] + ordered[half]) / 2
    bits = 0
    for value in coeffs:
        bits = (bits << 1) | (1 if value > median else 0)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    """Differing bits between two hex hashes."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _open_cover(file_path):
    """The first page of an archive as a fully decoded PIL image, or None."""
    from PIL import Image

    from core.thumbnail_cache import sorted_page_names

    lowered = file_path.lower()
    if lowered.endswith((".cbz", ".zip")):
        import zipfile
        opener = zipfile.ZipFile
    elif lowered.endswith((".cbr", ".rar")):
        import rarfile
        opener = rarfile.RarFile
    else:
        return None

    with opener(file_path, "r") as archive:
        pages = sorted_page_names(archive.namelist())
        if not pages:
            return None
        with archive.open(pages[0]) as page:
            img = Image.open(page)
            img.load()  # PIL is lazy; decode before the archive closes.
            return img


def image_hashes(img) -> list:
    """Hashes to search for one cover image, most likely first.

    A wraparound cover is the back cover on the left and the front on the
    right; Metron stores only the front. So a landscape page is split exactly
    at the midpoint (moving the cut a pixel changes the hash) and the right
    half is tried first, then the left (a right-to-left book's front), then
    the whole page.
    """
    width, height = img.size
    if not width or not height:
        return []
    if width / height > SPREAD_RATIO:
        mid = width // 2
        candidates = [
            img.crop((mid, 0, width, height)),
            img.crop((0, 0, mid, height)),
            img,
        ]
    else:
        candidates = [img]
    out = []
    for candidate in candidates:
        value = phash_hex(candidate)
        if value not in out:
            out.append(value)
    return out


def cover_hashes(file_path: str) -> list:
    """Hashes of a comic file's cover, or ``[]`` when it cannot be read.

    Never raises: a file whose cover cannot be hashed is matched by name, as
    every file was before.
    """
    try:
        img = _open_cover(file_path)
        if img is None:
            return []
        return image_hashes(img)
    except Exception as e:
        app_logger.debug(f"Cover hash failed for {os.path.basename(file_path)}: {e}")
        return []
