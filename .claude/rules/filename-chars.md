---
paths:
  - "core/filename_chars.py"
  - "cbz_ops/rename.py"
  - "cbz_ops/smart_rename.py"
  - "helpers/**"
  - "models/cbl.py"
  - "static/js/clu-utils.js"
  - "routes/metadata.py"
  - "routes/reading_lists.py"
  - "rename.py"
---

# Filesystem-hostile characters (the one sanitizer)

### Filesystem-Hostile Characters

`core/filename_chars.py` is the **only** sanitizer for a name CLU writes to
disk. `cbz_ops.rename.apply_filename_cleanup` (files),
`helpers.sanitize_path_segment` (folders: subscribe, split, auto-move) and
`models.cbl.format_search_term` (reading-list matching and the Map Issue modal)
all go through `apply_char_map`. There used to be three: files removed `/`,
folders turned it into `-`, and the matcher did neither — so
"Armageddon / X-Men" lived in `Armageddon - X-Men/` as `Armageddon X-Men 001`
and could never be mapped (#588).

- **The map is per character** (`rename_char_replacements`, a JSON
  `{char: replacement}`). A hostile character (`FILENAME_ILLEGAL_CHARS`) is
  **removed unless mapped** (#421). An install that has never saved the map
  gets `DEFAULT_CHAR_MAP` (`':'` → `' -'`): a colon sits between the words of
  a title, and removing it re-renamed every `Title - Subtitle` file the client
  renamer had produced for years. A saved map — even one that removes `:` — is
  honoured as-is. The map can change what replaces one but
  never keep one, and `normalise_char_map` strips hostile characters out of a
  replacement, because `str.translate` is one pass and a hostile character
  inside a replacement would reach the disk.
- **Do not pre-sanitize a value before it reaches the map.** The metadata
  renamer and Smart Rename used to turn `:` into ` -` and strip `/` first, so
  the user's setting for those two never applied on the commonest rename path.
- The legacy `rename_clean_specials_*` keys are read only until the settings
  page is saved once (`_legacy_char_map`); nothing writes them any more.
- Two JS mirrors, both in `static/js/clu-utils.js`: `CLU.applyCharMap`
  (`apply_char_map` + `normalise_char_map`) and `CLU.applyFilenameCleanup`
  (`apply_filename_cleanup` — collapse whitespace, then the map, then the
  spaces option). The Map Issue modal does **not** mirror either — it asks
  `/api/reading-lists/search-term`, so the modal and the automatic matcher
  cannot disagree.
- **A single-file metadata fetch renames on the client, so the map travels in
  `rename_config`.** `/api/search-metadata` applies ComicInfo.xml server-side
  and leaves the rename to `CLU.buildRenamedName`, whose output goes straight
  to `POST /rename` — it is the name on disk, not a preview. It carried its own
  `:` → ` -` swap and `[<>"/\|?*]` strip and never touched the map, so tagging
  a file from the File Manager and tagging it through
  `rename_comic_from_metadata` produced two different names for one series —
  the #588 divergence again, one layer up. `_rename_config_for` therefore sends
  a `cleanup` block (`routes/metadata._filename_cleanup_for_client`) and
  `buildRenamedName` ends at `CLU.applyFilenameCleanup`, exactly where
  `rename_comic_from_metadata` ends at `apply_filename_cleanup`. A payload
  without the block cleans to the defaults rather than to a second rule set.
  Asserted structurally in `tests/unit/test_filename_chars.py::TestMirrors`,
  because the renamer is JavaScript.
- `CLU.padIssueNumber` still hardcodes a width of 3 while the server reads
  `load_issue_pad_width()`. Pre-existing, and the next thing to send in that
  block.
