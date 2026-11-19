"""core.cover_hash reproduces imagehash.phash on Pillow alone.

Metron stores ``imagehash.phash()`` of each issue's cover and searches it by
Hamming distance. CLU computes the same hash without numpy/scipy, so the
expected values below were produced once by the real library (imagehash 4.3.2)
on the images this file draws. A change that moves one of them changes what
CLU sends Metron for every comic.
"""
import zipfile

from PIL import Image, ImageDraw

from core import cover_hash

SHAPES = {
    "blocks": [("rectangle", (40, 60, 380, 420), (200, 30, 30)),
               ("ellipse", (250, 500, 620, 930), (20, 60, 190)),
               ("rectangle", (0, 880, 650, 1000), (240, 220, 40))],
    "rings": [("ellipse", (60, 80, 590, 610), (250, 250, 250)),
              ("ellipse", (160, 180, 490, 510), (10, 10, 10)),
              ("rectangle", (90, 700, 300, 950), (30, 160, 90))],
}

# str(imagehash.phash(_draw(name)))
IMAGEHASH = {
    "blocks": "ea1595628d53936e",
    "rings": "e4b14ade9f603968",
}
IMAGEHASH_SPREAD = "cc6cb143a33c1d5b"  # rings on the left, blocks on the right


def _draw(name, size=(650, 1000)):
    img = Image.new("RGB", size, (90, 90, 110))
    d = ImageDraw.Draw(img)
    for kind, box, colour in SHAPES[name]:
        getattr(d, kind)(box, fill=colour)
    return img


def _spread():
    spread = Image.new("RGB", (1300, 1000))
    spread.paste(_draw("rings"), (0, 0))
    spread.paste(_draw("blocks"), (650, 0))
    return spread


def _cbz(path, pages):
    with zipfile.ZipFile(path, "w") as zf:
        for name, img in pages:
            if img is None:
                zf.writestr(name, b"not an image")
                continue
            with zf.open(name, "w") as fh:
                img.save(fh, format="PNG")
    return str(path)


class TestPhash:

    def test_matches_imagehash(self):
        for name, expected in IMAGEHASH.items():
            assert cover_hash.phash_hex(_draw(name)) == expected

    def test_matches_imagehash_on_a_landscape_image(self):
        assert cover_hash.phash_hex(_spread()) == IMAGEHASH_SPREAD

    def test_is_sixteen_lowercase_hex_characters(self):
        value = cover_hash.phash_hex(Image.new("L", (10, 10), 0))
        assert len(value) == 16
        assert value == value.lower()
        int(value, 16)

    def test_mode_does_not_matter(self):
        img = _draw("blocks")
        assert cover_hash.phash_hex(img.convert("RGBA")) == IMAGEHASH["blocks"]


class TestHamming:

    def test_counts_differing_bits(self):
        assert cover_hash.hamming("c585eb18bf1e5423", "c585eb18bf1e5423") == 0
        assert cover_hash.hamming("c585eb18bf1e5423", "c585eb18bf1e5422") == 1
        assert cover_hash.hamming("c585eb18bf1e5423", "c585eb18bf1e542c") == 4
        assert cover_hash.hamming("c585eb18bf1e5423", "3a7a14e740e1abdc") == 64

    def test_is_case_insensitive(self):
        assert cover_hash.hamming("C585EB18BF1E5423", "c585eb18bf1e5423") == 0


class TestImageHashes:

    def test_a_portrait_cover_is_hashed_whole(self):
        assert cover_hash.image_hashes(_draw("blocks")) == [IMAGEHASH["blocks"]]

    def test_a_spread_is_split_at_the_midpoint_front_half_first(self):
        # Metron stores only the front cover, which is the right half of a
        # wraparound; the left half is the front of a right-to-left book.
        assert cover_hash.image_hashes(_spread()) == [
            IMAGEHASH["blocks"], IMAGEHASH["rings"], IMAGEHASH_SPREAD,
        ]


class TestCoverHashes:

    def test_hashes_the_first_page_in_reading_order(self, tmp_path):
        path = _cbz(tmp_path / "a.cbz", [
            ("002.png", _draw("rings")),
            ("001.png", _draw("blocks")),
            ("ComicInfo.xml", None),
        ])
        assert cover_hash.cover_hashes(path) == [IMAGEHASH["blocks"]]

    def test_skips_the_junk_the_thumbnail_skips(self, tmp_path):
        path = _cbz(tmp_path / "a.cbz", [
            ("__MACOSX/000.png", _draw("rings")),
            ("001.png", _draw("blocks")),
        ])
        assert cover_hash.cover_hashes(path) == [IMAGEHASH["blocks"]]

    def test_unreadable_cover_is_no_hash_not_an_error(self, tmp_path):
        path = _cbz(tmp_path / "a.cbz", [("001.png", None)])
        assert cover_hash.cover_hashes(path) == []

    def test_no_pages_missing_file_and_other_types(self, tmp_path):
        assert cover_hash.cover_hashes(_cbz(tmp_path / "a.cbz", [("x.txt", None)])) == []
        assert cover_hash.cover_hashes(str(tmp_path / "gone.cbz")) == []
        assert cover_hash.cover_hashes(str(tmp_path / "book.pdf")) == []
