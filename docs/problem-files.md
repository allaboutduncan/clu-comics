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
