# Problem Files

Comics go bad. A download truncates, a scan bit-rots on disk, an archive turns
out to be a RAR wearing a `.cbz` name. Until now the only sign was a grey
placeholder where the cover should be, and an error line in a log that scrolls
away.

**Problem Files** is the page that collects those failures. It is under the gear
menu, next to Settings, and is visible to the Store Owner only.

## What appears here

A file is listed when an operation CLU actually ran could not read it:

| Source | What failed |
| --- | --- |
| **Thumbnail** | CLU could not render the cover |
| **Rebuild** | a rebuild could not finish |
| **Metadata write** | the file was re-tagged, but some pages failed their checksum and were copied unverified |

One file can appear more than once. A comic whose first page is damaged *and*
whose metadata write hit bad checksums is broken in two different ways, and each
gets its own entry with its own actions.

## It does not scan your library

CLU never goes looking for damage. A file appears here because something tried
to open it and could not — browsing a folder, generating a cover, re-tagging a
comic. An empty page means nothing has failed **yet**, not that your library has
been checked and is clean.

This keeps the page honest and cheap: no background job walks tens of thousands
of comics reading every archive.

## What the page tells you

Each row gives a plain-English cause rather than the raw error, and the action
most likely to help. The recommended action is the highlighted button.

| What you see | What it means |
| --- | --- |
| A page inside the archive is physically damaged | The file list is intact; the page data no longer matches its checksum |
| The compressed data stream is damaged | Usually an interrupted download, or bit rot |
| Not a ZIP archive at all | Almost always a RAR renamed `.cbz` |
| The archive contains no images | Not corruption — possibly a PDF or text file with the wrong extension |
| CLU could not write the thumbnail to its cache | **The comic is fine.** A `/cache` permissions problem — check PUID/PGID |

The raw error is always available behind **Details**.

## The actions

**Retry** re-runs the operation that failed. Offered for thumbnail entries,
where it is a single fast read.

**Search** looks for a replacement across your configured download sources —
the same GetComics, Usenet and DC++ search the Wanted and Series pages use. The
series, issue and year are read from the filename, so the box arrives
pre-filled. See [Replacing a damaged file](#replacing-a-damaged-file).

**Rebuild** unpacks the comic and repacks it as a fresh CBZ.

> **Rebuild only fixes one kind of damage.** It repairs an archive that is
> really a RAR with a `.cbz` name. It **cannot** recreate bytes lost to a bad
> checksum — it reads the same damaged data and stops at the same page. The
> page demotes the button and says so on any entry where it will not help. If a
> rebuild fails, your comic is left exactly as it was.

**Delete** moves the file to the Trash, where it can be restored.

**Dismiss** hides an entry without touching the file — for damage you know
about and accept. It stays hidden until the file itself is rewritten and still
fails; a dismissed entry will not keep resurfacing just because CLU read the
file again.

## Replacing a damaged file

When you search from a row, CLU already knows where the replacement belongs:
the damaged file's own path. Queue a download and it is filed straight onto it,
without you having to find and move anything.

This matters because a damaged comic is not a *missing* one. The normal
downloading pipeline files issues you do not have; a corrupt file is still a
file, so a replacement would otherwise sit in your processed folder forever.

While the download is in flight the row shows **Replacing**, and a banner tracks
it. The swap happens on its own when the file lands — including if you close the
page.

Three things happen before anything is overwritten:

- **The replacement is checked first.** Every entry's checksum is verified and
  the archive must actually contain pages. A replacement that is itself damaged
  is refused, and your original is left alone — a partly readable comic is worth
  more than a broken one.
- **The damaged copy goes to the Trash**, not straight to deletion. A swap you
  did not want costs a restore, not a re-download.
- **A `.cbr` will not replace a `.cbz`.** If the download has not been converted
  yet the entry simply waits, rather than downgrading your library.

If any step fails, the entry says why and nothing is changed.

## Unreachable entries

An entry marked **Unreachable** means CLU cannot see the file *or* the folder it
lives in — almost always a library that is not mounted. Those entries are kept,
not cleaned up, because a sleeping NAS should not empty this list. Only
**Remove** is offered until the library is back.

Entries whose file has genuinely been deleted clear themselves the next time the
page loads.

## When the list will not load

If the page reports that entries are recorded but could not be listed, the
database could not be read — check the logs and the Database section under
Settings. The page will not tell you nothing has failed unless it knows that to
be true.

## Library health scan

By default nothing looks for damage on its own, so an empty list means *nothing
has failed*, not *the library is clean*. The **Library health** card at the top of
the page changes that — it is **off by default**, because it reads every comic.

Turn on **Scan automatically** (or press **Run scan now**) and CLU checks a batch
of files at a time, stalest first, so a large library is covered over days
without hammering the disks. Each file is hashed and, for `.cbz`, every page's
checksum is verified; everything is re-checked every 90 days. The bar shows how
much of the library has a current result.

It adds three kinds of entry to this list:

- **Integrity scan** — a `.cbz` whose checksums or pages fail. Treated like any
  other damaged file: **Search** for a replacement, or **Delete**.
- **Stray files** — a `.cbr` sitting beside a `.cbz` of the same name, or a
  comic that has been in your download folders for over a day without being
  filed. Nothing is wrong with the file; **Dismiss** an entry you meant to keep.
- **Empty folders** — a library folder with no comics in it: nothing at all, or
  only what CLU leaves behind (`cvinfo`, `series.json`, folder art). Typically what
  is left after a series was moved or deleted elsewhere. **Delete** sends the
  folder and its leftovers to the Trash (a completely empty or `cvinfo`-only
  folder is removed outright); CLU looks again first and refuses if a comic has
  appeared since. A folder holding anything else (a `cover.jpg`, a text file) is
  never listed, a library root never is, and when a whole branch is empty only its
  top folder is listed. The **Scan empty folders** button in the page header runs
  just this check, without hashing anything; the **Empty folders** filter shows
  only these entries.

**Review duplicates** lists files with identical content (*exact*) and files
tagged as the same series, issue and year *and* named alike (a tag that contradicts the filename, such as an event's spin-offs all tagged as the parent series, is not treated as a duplicate), or one folder's names that differ only
by a copy marker like `(1)` (*likely* — they may be variants, so check). Pick the
copy to keep; the others go to the Trash. Nothing is removed automatically.

The score beside **Problem Files** in the gear menu is 100 minus a capped penalty
for open damage, duplicate groups and strays. It turns red when a file has failed
its integrity check. If a library is not mounted, CLU skips it rather than
reporting its files as damaged.

## Refreshing every series.json at once

When a provider outage or a settings change leaves many `series.json` files with a
blank name, description or cover, the **Refresh all series.json** button (shown in
the page header while any such entry is open) refreshes all of them in the
background instead of one row at a time. It works through the entries one folder at
a time using your provider order, keeps any fields you edited by hand, and skips
entries you dismissed.

A folder only counts as corrected if its `series.json` passes the check afterwards.
When the run finishes, a panel lists every folder that **still needs attention**
and why &mdash; for example no provider returned data, or the provider answered
without a description &mdash; and those entries stay in the list. The same summary
is written to the application log.
