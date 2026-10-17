---
paths:
  - "core/database.py"
  - "app.py"
  - "routes/files.py"
  - "routes/collection.py"
  - "core/file_watcher.py"
  - "cbz_ops/**"
  - "monitor.py"
---

# Path references and deletion hooks

### Path References

Six columns hold an absolute comic path as a bare string, with no foreign key to
`file_index`, so every one of them is orphaned independently when a file moves:

| Table | Column(s) |
|-------|-----------|
| `file_metadata_tags` | `file_path` |
| `reading_positions` | `comic_path` |
| `issues_read` | `issue_path` |
| `reading_list_entries` | `matched_file_path`, `manual_override_path` |
| `reading_lists` | `thumbnail_path` |

`core.database.move_path_references(old, new, is_dir=, conn=)` is the **one**
body for "the file moved, follow it", and `clear_path_references(path)` for "the
file is gone, let go of it". There used to be two drifting copies of the first —
`move_reading_data` and an inline block inside `update_file_index_entry` — which
is exactly why the directory branch never rewrote `file_metadata_tags`. Do not
add a third; `update_file_index_entry` and `update_index_on_move`'s directory
branch both delegate.

Reading lists are the reason `manual_override_path` is on that list as well as
the auto column. `rematch_entries` deliberately **skips** an entry that has a
manual override — a hand-picked mapping is the user's answer, not the matcher's
— so a manual mapping broken by a rename can never heal itself. Following the
path is the only thing that fixes it.

Things that look arbitrary and are not:

- **`move_path_references` takes an injected `conn`, and `update_file_index_entry`
  passes its own.** Opening a second connection while the first still holds the
  WAL writer lock blocks for the full 30s `busy_timeout` and then fails — on
  every single rename.
- **`OR REPLACE` is only for the three tables with a uniqueness constraint on
  the path** (`reading_positions UNIQUE(user_id, comic_path)`,
  `issues_read.issue_path` UNIQUE, `file_metadata_tags PRIMARY KEY(file_path,
  kind, value)`). The two reading-list tables have none, and writing
  `OR REPLACE` there would read as though they did.
- **`file_metadata_tags` needed `OR REPLACE` all along.** Its composite primary
  key meant renaming onto a path that already had tag rows raised
  `IntegrityError` inside the one `try` that also performs the `file_index`
  rename — so the rename was silently lost and logged as a generic error.
- **The trailing slash in `'{old}/%'` is load-bearing** — `'{old}%'` also
  rewrites `/data/Batman Beyond` when you move `/data/Batman`.
- **`clear_path_references` has no `is_dir` flag.** At delete time the path is
  already gone, so an `os.path.isdir` test is permanently False; the exact match
  and the prefix sweep both run unconditionally, and the `LIKE` arm is a free
  no-op for a file. `delete_file_index_entry` uses the same shape for the same
  reason.
- **A delete does NOT clear `reading_positions` or `issues_read`.** "I read
  this" is a fact about the user, not about the file, and a trashed comic can be
  restored.
- **`idx_rle_matched_path` / `idx_rle_override_path` serve the `= ?` arms only.**
  SQLite's `LIKE` is case-insensitive by default and cannot use a BINARY-collated
  index. Do **not** "fix" that with `COLLATE NOCASE`: these paths are stored
  byte-exact so they join against `file_index.path`.

#### Deletion hooks — the three callers that are not deletions

`forget_deleted_path` / `forget_deleted_paths` pair the index delete with the
mapping clear, and they are wired at the **deliberate deletion sites**
(`app.update_index_on_delete`, `routes/files.delete_multiple`,
`core/file_watcher.py`) — *not* behind a flag on `delete_file_index_entry`.
Three of that function's callers delete a row as part of something that is not a
deletion at all, and must keep calling it directly:

- `cbz_ops/single_file.py` — CBR→CBZ is a rename wearing a delete's clothes:
  delete the old row, add the new one, then `move_path_references`.
- `app.py`'s post-download tidy-up — dropping a stale row after a ComicVine
  rename.
- `routes/collection.py` — a folder **re-scan**, which deletes the subtree and
  immediately re-adds it. Clearing here would wipe every reading-list match
  under that folder on every rescan.
