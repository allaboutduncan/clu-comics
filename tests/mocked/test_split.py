"""Tests for cbz_ops/split.py -- split a multi-issue CBZ into single issues."""
import io
import os
import zipfile
import pytest


def _png_bytes(color=(100, 100, 200)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (50, 75), color=color).save(buf, format="PNG")
    return buf.getvalue()


def _make_multi_issue_cbz(path, issues, series="Cult of Dracula", nested=False):
    """issues: dict of issue_str -> page_count. Names pages '<series> <issue> - NNNN.png'."""
    prefix = "wrapper/" if nested else ""
    with zipfile.ZipFile(str(path), "w") as zf:
        for issue, pages in issues.items():
            for p in range(1, pages + 1):
                zf.writestr(f"{prefix}{series} {issue} - {p:04d}.png", _png_bytes())
    return str(path)


class TestParseIssueKey:

    def test_canonical(self):
        from cbz_ops.split import parse_issue_key
        assert parse_issue_key("Cult of Dracula 003 - 0001.jpg") == "003"

    def test_decimal_issue(self):
        from cbz_ops.split import parse_issue_key
        assert parse_issue_key("Series 003.1 - 0002.png") == "003.1"

    def test_spacing_variants(self):
        from cbz_ops.split import parse_issue_key
        assert parse_issue_key("Series 4 -0007.jpg") == "4"
        assert parse_issue_key("Series 4-0007.jpg") == "4"

    def test_non_matching(self):
        from cbz_ops.split import parse_issue_key
        assert parse_issue_key("page_001.jpg") is None
        assert parse_issue_key("cover.jpg") is None


class TestDetectGroups:

    def test_multi_issue_consecutive(self):
        from cbz_ops.split import detect_groups
        rels = [
            "Cult of Dracula 003 - 0001.png",
            "Cult of Dracula 003 - 0002.png",
            "Cult of Dracula 004 - 0001.png",
            "Cult of Dracula 005 - 0001.png",
            "Cult of Dracula 005 - 0002.png",
        ]
        groups = detect_groups(rels)
        assert [g["issue_key"] for g in groups] == ["003", "004", "005"]
        assert [len(g["page_rel_paths"]) for g in groups] == [2, 1, 2]

    def test_all_none_single_group(self):
        from cbz_ops.split import detect_groups
        rels = ["page_001.png", "page_002.png", "cover.png"]
        groups = detect_groups(rels)
        assert len(groups) == 1
        assert len(groups[0]["page_rel_paths"]) == 3

    def test_leading_unmatched_absorbed(self):
        from cbz_ops.split import detect_groups
        rels = [
            "0000 intro.png",       # no issue key
            "Series 003 - 0001.png",
            "Series 003 - 0002.png",
        ]
        groups = detect_groups(rels)
        assert len(groups) == 1
        assert groups[0]["issue_key"] == "003"
        assert len(groups[0]["page_rel_paths"]) == 3


class TestExtractForSplit:

    def test_leaves_original_and_extracts(self, tmp_path):
        from cbz_ops.split import extract_for_split
        cbz = _make_multi_issue_cbz(tmp_path / "collection.cbz", {"003": 2, "004": 1})
        result = extract_for_split(cbz)

        assert os.path.isfile(cbz)  # original untouched
        folder = result["folder_name"]
        pngs = [f for _, _, fs in os.walk(folder) for f in fs if f.endswith(".png")]
        assert len(pngs) == 3
        assert os.path.isdir(result["root_folder"])

    def test_collapses_nested_wrapper(self, tmp_path):
        from cbz_ops.split import extract_for_split
        cbz = _make_multi_issue_cbz(tmp_path / "nested.cbz", {"001": 2}, nested=True)
        result = extract_for_split(cbz)
        # folder_name should point at the wrapper contents, not the outer temp dir
        entries = os.listdir(result["folder_name"])
        assert any(e.endswith(".png") for e in entries)


class TestCommitSplit:

    def test_writes_issue_files(self, tmp_path):
        from cbz_ops.split import commit_split
        folder = tmp_path / "extracted"
        folder.mkdir()
        (folder / "Series 003 - 0001.png").write_bytes(_png_bytes())
        (folder / "Series 003 - 0002.png").write_bytes(_png_bytes())
        (folder / "Series 004 - 0001.png").write_bytes(_png_bytes())

        out_dir = tmp_path / "Series"
        outputs = commit_split(str(folder), str(out_dir), [
            {"output_name": "Series 003", "rel_paths": ["Series 003 - 0001.png", "Series 003 - 0002.png"]},
            {"output_name": "Series 004", "rel_paths": ["Series 004 - 0001.png"]},
        ])

        assert len(outputs) == 2
        assert outputs[0]["total_images"] == 2
        for o in outputs:
            assert os.path.isfile(o["path"])
            with zipfile.ZipFile(o["path"]) as zf:
                names = zf.namelist()
                assert not any(n.lower().endswith("comicinfo.xml") for n in names)

    def test_dedup_duplicate_basenames(self, tmp_path):
        from cbz_ops.split import commit_split
        folder = tmp_path / "extracted"
        (folder / "sub").mkdir(parents=True)
        (folder / "img.png").write_bytes(_png_bytes())
        (folder / "sub" / "img.png").write_bytes(_png_bytes())

        out_dir = tmp_path / "out"
        outputs = commit_split(str(folder), str(out_dir), [
            {"output_name": "Issue 1", "rel_paths": ["img.png", os.path.join("sub", "img.png")]},
        ])
        with zipfile.ZipFile(outputs[0]["path"]) as zf:
            names = sorted(zf.namelist())
        # Mirrors combine_cbz: the first keeps its name, later duplicates get b, c, ...
        assert names == ["img.png", "imgb.png"]

    def test_collision_suffix(self, tmp_path):
        from cbz_ops.split import commit_split
        folder = tmp_path / "extracted"
        folder.mkdir()
        (folder / "Series 003 - 0001.png").write_bytes(_png_bytes())
        out_dir = tmp_path / "Series"
        out_dir.mkdir()
        (out_dir / "Series 003.cbz").write_bytes(b"existing")

        outputs = commit_split(str(folder), str(out_dir), [
            {"output_name": "Series 003", "rel_paths": ["Series 003 - 0001.png"]},
        ])
        assert outputs[0]["name"] == "Series 003 (1).cbz"
