"""core.cbl_repos: parsing repo specs and building GitHub URLs."""
import pytest

from core.cbl_repos import (
    html_url,
    normalize_repo,
    raw_file_url,
    split_repo,
    tree_api_url,
)


@pytest.mark.parametrize("text, spec", [
    ("DieselTech/CBL-ReadingLists", "DieselTech/CBL-ReadingLists"),
    ("  owner/repo  ", "owner/repo"),
    ("owner/repo@main", "owner/repo@main"),
    ("owner/repo@feature/x", "owner/repo@feature/x"),
    ("https://github.com/owner/repo", "owner/repo"),
    ("https://github.com/owner/repo/", "owner/repo"),
    ("github.com/owner/repo.git", "owner/repo"),
    ("https://www.github.com/owner/repo/tree/dev", "owner/repo@dev"),
    ("https://github.com/owner/repo/tree/feature/x", "owner/repo@feature/x"),
])
def test_normalize_accepts(text, spec):
    assert normalize_repo(text) == spec


@pytest.mark.parametrize("text", [
    None, "", "owner", "owner/", "/repo", "owner/repo/extra", "own er/repo",
    "owner/repo@", "owner/repo@a,b", "owner/repo@../x", "owner/..",
    "https://gitlab.com/owner/repo", "https://evil.com/github.com/owner/repo",
])
def test_normalize_rejects(text):
    assert normalize_repo(text) is None


def test_split_defaults_to_head():
    assert split_repo("o/r") == ("o/r", "HEAD")
    assert split_repo("o/r@dev") == ("o/r", "dev")


def test_urls():
    assert tree_api_url("o/r") == "https://api.github.com/repos/o/r/git/trees/HEAD?recursive=1"
    assert tree_api_url("o/r@a/b") == "https://api.github.com/repos/o/r/git/trees/a/b?recursive=1"
    assert raw_file_url("o/r", "X/Part #01 [a].cbl") == \
        "https://raw.githubusercontent.com/o/r/HEAD/X/Part%20%2301%20%5Ba%5D.cbl"
    assert html_url("o/r") == "https://github.com/o/r"
    assert html_url("o/r@dev") == "https://github.com/o/r/tree/dev"


def test_get_repos_dedupes_and_skips_invalid(monkeypatch):
    import core.cbl_repos as cbl_repos
    monkeypatch.setattr(cbl_repos, "get_setting",
                        lambda key: "a/b, A/B ,junk,,c/d@x")
    assert cbl_repos.get_repos() == ["a/b", "c/d@x"]


def test_blank_setting_means_no_repos(monkeypatch):
    import core.cbl_repos as cbl_repos
    monkeypatch.setattr(cbl_repos, "get_setting", lambda key: "")
    assert cbl_repos.get_repos() == []
