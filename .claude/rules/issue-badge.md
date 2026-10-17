---
paths:
  - "static/js/collection.js"
  - "static/js/reading_list.js"
  - "static/js/reader.js"
  - "templates/collection.html"
  - "routes/collection.py"
  - "core/database.py"
---

# Collection grid issue badge and read-status wiring

#### The Issue Badge, and Why the Read Icon Is Not Inside Its `if`

One report described three symptoms — read status not showing, issue 008 badged
`#1` while 001 badged `#1` too, and issue 007 badged not at all. It was one badge
bug plus three read-status wiring bugs, all in `static/js/collection.js`.

- **The read icon is a *child* of `.issue-badge`** (`templates/collection.html`),
  and the badge used to be un-hidden only inside `if (issueNum)`. So a filename
  the parser could not read hid the user's read status as well — "no badge" and
  "read status not showing" were the same defect. The badge is now unconditional
  for a comic and only `.issue-number` is conditional; an unnumbered badge gets
  `.issue-badge-unnumbered` so the icon's left margin collapses. Do not put the
  display back inside a branch on the number.
- **The number's source of truth is `file_index.ci_number`** (ComicInfo
  `<Number>`, stored verbatim), carried by `get_directory_children` →
  `/api/browse` and `get_files_recursive_paged` → `/api/browse-recursive`.
  `''` is a real stored value — a file tagged without a `<Number>` — so
  `routes.collection._badge_issue_number` turns blank into null at the **API
  boundary** for both routes; the database helpers pass the column through raw,
  like `has_comicinfo`. A bare `#` is worse than falling back to the filename.
- **The JS fallback is deliberately *not* `cbz_ops.rename.extract_comic_values`.**
  That parser logs at INFO on every match (nine sites), so calling it once per
  row of a directory listing is precisely the per-item-INFO mistake recorded
  under **Logging**. `extractIssueNumber`'s rules are: an explicit `#`/`Issue`
  marker wins wherever it sits, otherwise the **first** bare number token wins —
  every trailing number in a comic filename (a subtitle, a cover count, a
  mini-series count) comes *after* the issue number and never before it. The old
  set was unanchored and merely required the digits to sit next to a `(YYYY)`,
  which is how `008 - Book 1 (2005)` and `001 (2005)` both badged `#1`.
  A series name ending in a bare number (`Batman 66 001`) is unresolvable from
  the filename and is what `ci_number` is for; `1000000` is spelled out for the
  same reason `ONE_MILLION_ISSUE_PATTERN` is.
- **Leading zeros are stripped once, in `stripIssueZeros`**, mirroring
  `cbz_ops.rename._strip_issue_zeros`, so a tagged `8` and an untagged `008`
  cannot sit side by side reading `#8` and `#008`. Never `padStart`: it turns
  `-1` into `0-1`.
- **`renderPage` is shared by ~7 feeder modes**, each mapping a differently
  shaped payload into `allItems`. Only the two browse routes supply `ciNumber`;
  anything server-supplied must degrade to the filename where a mode omits it.
- **`window._readerReadIssuesSet` must be published inside the fetch callback,
  not at parse time.** `readIssuesSet = new Set(...)` *rebinds* the variable, so
  a reference taken earlier is an orphan and `reader.js` marks issues read into a
  Set nobody reads — the comment claiming it was "never reassigned" was false.
  `collection.js` and `reading_list.js` both publish it in `loadReadIssues`,
  beside the `applyReadIconsToGrid()` back-fill that closes the race with the
  first render (`loadDirectory()` and that fetch both start from
  `DOMContentLoaded`, and nothing used to reconcile the grid when the fetch
  lost). The back-fill iterates the **grid** and asks the Set, not the reverse:
  the Set can hold tens of thousands of paths.
- **A page hosting `reader.js` owes it `window._readerOnMarkedRead`.**
  `collection.js` never defined it, so finishing a comic in the reader did not
  flip the on-screen icon.
- `applyReadStateToGridItem` is the one flip — icon plus the two menu labels that
  depend on it — called from `renderPage`, `updateReadIcon` and the back-fill, so
  the badge and the dropdown cannot disagree.

`tests/unit/test_issue_badge.py` pulls the pattern sources out of the `.js` and
runs the filename table through Python's `re` (the patterns are written in the
subset both engines share, and the table was verified identical under node), and
asserts the wiring above structurally — none of the three read-status defects is
visible to a test that does not read the file.
