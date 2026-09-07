# Photos

Copies your iCloud photo library onto an external drive so you can delete the originals from iCloud and
get the storage back. Part of [my-cloud](../README.md); read that first for setup and the web shell.

The backup only ever grows. Delete a photo from iCloud and the local copy stays exactly where it is, its
row marked as no longer in iCloud, and the drive becomes the only place that photo exists. Nothing in
here deletes anything, from iCloud or from the drive.

Photos, videos, GIFs, whatever else Apple stores: all one thing here, an asset with original bytes worth
keeping. There is no per-type branch anywhere in the code.

Once assets are on the drive you can browse and filter them at `/photos` in the web UI. See
[The gallery](#the-gallery).

This tool never runs on the deploy target, which is why its dependencies sit in a `photos` group that a
plain `uv sync` skips. It borrows the main app's Apple credentials and session, so a session already
trusted by `uv run mycloud devices` will not ask for 2FA again.

## Quick start

```bash
uv sync --group photos

# What would a download fetch? Counts, sizes, and how the drive compares to iCloud
uv run --group photos python -m icloud_photo_metadata plan

# Fetch it
uv run --group photos python -m icloud_photo_metadata download

# Check the backup is intact. Do this before deleting anything from iCloud
uv run --group photos python -m icloud_photo_metadata verify

# What would deleting the marked assets take? Prints the plan and stops
uv run --group photos python -m icloud_photo_metadata prune

# Carry it out
uv run --group photos python -m icloud_photo_metadata prune --yes
```

There is a fifth command, `import-manifest`, which you run once on a drive that predates the database
and then never again.

Nothing is configurable. The backup directory is `DEFAULT_DIR` in `backup.py`, metadata always refreshes
before planning, and `verify` always hashes. A flag on any of those would only be a way of getting a
different answer to the same question.

`plan` and `download` do identical work and differ only in whether they carry it out, so the plan you
read is the plan that runs. `prune` is the same shape: without `--yes` it prints and stops. Both refresh metadata from iCloud first, which is why neither can ever report
against a stale view.

Figures throughout come from one real library: **9,711 assets, 26.6 GB of originals**, of which 9,618 are
eligible for backup. The drive holds 9,619 assets across 9,750 files, since a Live Photo is one asset with
two.

## How a download works

```
① refresh   iCloud ──► 4 pages, ~3s each ──► upsert assets, in one transaction
                       anything an exhaustive crawl missed ──► in_icloud = 0, row kept
                       any page failed? nothing is written, exit 1
                       an album moved further than the merge did? read all ~99 pages instead

② index     assets still in iCloud ──► eligible  (album is not Hidden or Recently Deleted)
              every file recorded for this id present and the right size? ──► already backed up
              otherwise                                                   ──► missing
            a file no row claims                                          ──► reported as a stray

③ apply     missing ──► download, check the length, hash it, stamp the mtime, record it
                        one commit per asset
                        10 failures in a row ──► stop, keep what landed, exit 1
```

Answering "is this backed up?" is a lookup by asset id, with no guessing and no probing. That works only
because the backup directory contains nothing but files this tool downloaded, one per asset, each named
with that asset's id.

## Storage

One SQLite database, sitting on the drive next to the media it describes. 53.5 MB for 9,711 assets.

| Table | Holds |
|-------|-------|
| `assets` | One row per asset iCloud has ever shown us, with `in_icloud` saying whether it still does |
| `asset_resources` | Per-variant size, dimensions and Apple checksum: original, medium, thumb, video halves |
| `files` | Asset id and kind, mapped to a path on the drive, its size and its sha256 |
| `marks` | What you flagged for deletion, where from, and when `prune` carried it out |
| `album_counts` | What iCloud reported for each album at the last pull, the baseline for the next one |
| `meta` | When the last pull happened |

`camera_make`, `camera_model`, `lens_model`, `iso`, `f_number` and `focal_length` are generated columns
computed from the `exif` blob, so filtering on any of them needs no re-pull. Adding another is a schema
change and nothing more. Indexing matters where it is used: the camera facet takes 0.9 ms with its index
and 88 ms without.

Media is filed by capture year:

```
icloud-backup/
├── library.db
├── library.db.dump
├── 2016/ … 2024/ (2,145)  2025/ (1,449)  2026/ (1,057)
└── IMG_1972~a1b2c3d4.HEIC + IMG_1972~a1b2c3d4.MOV   ← a Live Photo: one asset, two files
```

## Layout

```
icloud_photo_metadata/
├── cli.py                the commands, and every line this package prints
├── backup.py             the pipeline: index, plan, apply
├── library.py            the database on the drive: schema, connection, every query
├── queries.py            reads for the gallery: filter, sort, page, facet
├── prune.py              carrying the marks out: what may be deleted, and doing it
├── drive.py              list, stat, hash. Nothing else touches the filesystem
├── icloud_source.py      the only module that knows about pyicloud and CloudKit
├── pull_metadata.py      refreshing the library's view of iCloud
├── models.py             what an asset is, independent of how it is stored
└── migrate_manifest.py   the one-time JSON import. Delete once no drive needs it
```

`eligible`, `build_index`, `build_plan` and `backup_filename` all take plain values and return plain
values, so the tests covering them touch no database at all. Filesystem and network access is confined to
`drive.py`, `library.py` and `icloud_source.py`. `cli.py` owns all the printing, which is what lets the
pipeline be tested on its return values.

## Design decisions

### SQLite rather than JSON

This used to be two JSON files, and for a tool that only ever asked "what do I still owe?" that was the
right shape: both were read whole and written whole, with no queries and no concurrent writers.

Wanting to browse and filter the library is exactly the workload that assumption ruled out, so the
decision reverses on its own terms. It turned out to pay for itself elsewhere too. A grid page over 9,711
assets takes 0.6 ms. Loading every asset is faster than parsing the old JSONL was, 0.9 s against 1.4 s.
And the database is smaller than the two files it replaced, 53.5 MB against 63 plus 43 MB, because the
old manifest's per-record metadata snapshot was a second copy of the cache and is now gone.

### A pull never deletes a row

It clears `in_icloud` instead. Reclaiming quota is the whole point, so an asset leaving iCloud is the
expected end of its life here rather than something to forget. Once it has gone, its row is the only
thing that still describes it.

The whole pull is one transaction, so a partial one leaves the library exactly as it was. A run aborts
rather than plan against a pull that lost pages, because lost pages could be hiding entire assets.

### The library is keyed by Apple's asset UUID

Keying by id rather than path means renaming a file cannot break the link. A content hash cannot do that
job: it does not exist until after the file has been downloaded, so it cannot identify an asset that is
still only metadata, and a Live Photo would have two of them for one asset. `sha256` is a column instead,
which is what `verify` reads.

### The drive holds only what this tool put there

This is the invariant everything else rests on. It let us delete content matching, redundancy detection
and every ffprobe call, roughly 500 lines whose only job was working out what an unknown file was. A file
here that no row claims is now an anomaly a run reports, not a category it has to tally. Your ad-hoc
AirDrops and screen recordings stay in their own directory, untouched.

### One commit per asset

The old JSON manifest carried each asset's full metadata, so every save rewrote all 63 MB. Recording
9,617 downloads that way writes a triangular number of bytes, about 418 GB to record 25 GB, which is why
it batched every 100 records and carried extra bookkeeping to make batching safe.

A row insert costs the same whether the library holds ten assets or ten thousand, measured at 69 ms per
commit on this exFAT drive. The batching, the flush on exit, and the "you might lose 100 records in a
crash" caveat all disappeared with it.

### Named `IMG_4030~a1b2c3d4.HEIC`, filed by capture year

The capture date never changes, and splitting by year keeps the biggest directory near two thousand
files. The id fragment is unconditional because iPhone recycles `IMG_1234` numbering after a reset. There
are 1,324 such collisions in this library, so it is the common case rather than an edge one, and carrying
the id makes every name unique without searching for collisions first.

### The pull reads the front of the library, not all of it

Every page costs about the same 3 s whatever it holds, because the cost is Apple's round trip rather than
the payload. Page count is the only lever, and rank 0 is an album's newest asset, so the walk reads
Library pages until two in a row carry nothing new and then stops. Four pages instead of ninety-nine.

Hidden and Recently Deleted are crawled end to end every time regardless. They are one page between them,
and being certain of their membership is what keeps `source_album` truthful when something moves.

What the walk cannot see is a change deep in the library: an edit to an old asset, or an import whose
capture date sorts it past the pages read. So every pull records what iCloud said each album's length was,
and the next one escalates to a full pull if an album moved further than the merge accounts for. The check
compares deltas, never absolutes, because a reported album length counts rows its own pages never return
(9,620 against 9,618 here) and so can never be compared to a record count.

Two changes that cancel out, a deletion plus an old-dated import between runs, still slip past. Emptying
`album_counts` forces the full pull that catches them.

### Everything runs one request at a time

`_SESSION_LOCK` serializes every call through pyicloud's session, because each request rewrites the cookie
jar and the session JSON with a truncating `open()`. Concurrent reads could leave the very session file a
download depends on half-written.

That makes worker pools pointless: four pages take 12.5 s whether you use one worker, four, or eight.
Downloads are serial for that reason plus one of their own, which is that Apple paces the account rather
than the connection, so a pool only made every dropped connection everybody's problem.

### Rollback journalling, not WAL

The drive is exFAT and removable. WAL would strand `-wal` and `-shm` files beside the media if it were
unplugged mid-write, and with exactly one writer its concurrency buys nothing.

### Migrations only for the tables that cannot be re-pulled

`assets` and `asset_resources` are derived and can always be re-pulled in about 80 s, so a schema change
there is a rebuild rather than a migration. `files` and `marks` are not derived from anything: what this
drive holds and what you decided about it exist nowhere else.

So there is no framework, just `_MIGRATIONS` in `library.py` -- one SQL block per version step, run inside
the caller's transaction. `PRAGMA user_version` still refuses a library from a version this one cannot
reach. v1 to v2 split `marks` from a single `delete` into the levels below and added `executed_at`; v2 to
v3 widened the mark `CHECK` to admit `drive`, which SQLite can only do by rebuilding the table.

**`_SCHEMA` only builds a library, it never reshapes one.** Opening one that is already at
`SCHEMA_VERSION` runs no DDL, stamps nothing and opens no transaction. The drive is exFAT over USB, and
re-running the schema means a journal and two fsyncs -- 10 ms typical, just under two seconds at the tail,
paid once per marked photo by a gallery that opens a connection per click. The cost of that shortcut is
that every schema change needs a `_MIGRATIONS` step, the additive ones included.

A read-only open migrates too, by opening a writable connection first. It costs one `user_version` read on
a page view with nothing to do, and it is what stops the gallery reading v1 rows through v2 code.

### Failures re-open the session

Ten download failures in a row almost always mean one dead session rather than ten bad assets, so a
download re-authenticates and resumes with whatever it still owes. An overnight run survives however many
expiries it takes. Each asset is retried at most twice, so one asset failing for its own reasons cannot
stall every subsequent pass, and the run gives up only after three fresh sessions in a row have downloaded
nothing at all. Whatever landed stays recorded either way.

### Nothing overwrites

A file at a backup's path with the wrong size is reported as damage, never replaced. Hashing on write is
unconditional and `verify` always checks it, because a size check cannot catch a file that rots without
changing length.

### `verify` writes a plain-text copy

A clean run also writes `library.db.dump`, the SQL that recreates the database. It is 46 MB and takes 6 s,
which is nothing against a run that hashes 26.6 GB anyway.

The database is the only record of which bytes on the drive belong to which asset, and for anything
already freed from iCloud nothing else can rebuild it. The dump is the copy that needs no SQLite to read:
restore with `sqlite3 new.db < library.db.dump`, or failing that, grep it.

A failed verify deliberately skips the dump. Overwriting the copy taken while the library was whole with a
snapshot of a damaged one would be worse than having no copy.

### A prune fetches by record name, a download pages albums

Both need the live pyicloud object behind an id, and they get it two different ways on purpose.

A download wants most of the library, so paging albums newest-first is the cheapest way to reach hundreds
of assets at once. A prune wants three photos, and one of them is from 2018: paging would walk everything
newer to reach it.

The obvious answer, `album._get_photo(id)`, is a trap. Its indexed lookup misses on a smart album, and it
falls back to iterating the album -- measured at **289 s** for one asset against this library. Asking
CloudKit for the record itself takes **0.26 s** and does not care how old the asset is, so `fetch_asset`
does that: `records/lookup` for the `CPLAsset`, then its `masterRef` for the `CPLMaster`.

### `icloud_source.py` quarantines pyicloud

Reading this metadata means leaning on private methods, raw CKRecord fields, and the `mediaMetaDataEnc`
EXIF blob that pyicloud never decodes. Any patch release could move all of it. The rest of the tool sees
`PhotoMetadata` and nothing else.

## The gallery

`/photos` in the web UI is a thumbnail grid you can filter by camera, year, album, kind, size, video
length, favourite, location, and whether an asset is still in iCloud or backed up at all. Clicking a
thumbnail opens its EXIF, and marks it for deletion from either place its bytes sit, or from both.

The page lives in `src/web/photos/` and its queries in `queries.py`. Both need the drive mounted, and the
routes answer 503 when it is not, because an unplugged USB disk is a state to explain rather than a server
error.

**A mark says where the bytes should stop existing.** The detail panel offers the two places a copy sits
-- iCloud and this drive -- and each toggles on its own, so every combination is a mark: `icloud` frees the
iCloud copy and keeps the drive's, which is the whole point of having a backup; `drive` is its mirror; and
`everywhere` is both, the only thing in this tool that lets go of the last copy of a photo. Neither place
selected is no mark at all, which is why "not marked" has no button of its own.

**The filter bar can show what you marked**, by location or all of it, which is the question worth asking
before pruning.

**Marking does not recount the library.** The mark route answers with nothing but the mark. Totals mean a
scan of all 9,711 assets, which is 18 ms with the drive's pages cached and was measured at 3.7 s without
-- on the one control a person clicks over and over. The page moves its own count by the one mark that
changed.

**Nothing is deleted until a prune runs**, from the gallery's button or from `prune --yes`. Both build the
same plan and both refuse the same things, and what each gate checks is the copy that survives it. An
`iCloud` mark is skipped unless the drive holds every file the library recorded for that asset, at the
recorded size, hashing to the recorded digest. A `drive` mark is skipped unless iCloud still holds the
photo, but nothing is hashed: Apple's copy is not ours to verify. A skipped asset stays marked, so fixing
the reason and re-running picks it up. `Everywhere` is not gated at all: discarding the last copy is the
request.

**A deleted asset goes to Recently Deleted**, where Apple keeps it for 30 days. The prune sets `isDeleted`,
which is exactly what the Photos app's delete does. So an `iCloud` prune is recoverable for a month, and an
`everywhere` prune is recoverable for a month minus the drive's copy, which is gone for good. A `drive`
prune never touches Apple, so nothing of it is recoverable -- but the photo it leaves in iCloud is whole.

**A pruned asset keeps its row.** `in_icloud` goes to 0 the moment the delete lands rather than at the next
pull, and a prune that takes the drive's copy drops the `files` rows with the files. The metadata outlives
both copies,
which is the point of the library being on the drive.

**The gallery's prune runs on a background thread**, the same way the refresh does: each asset costs a
CloudKit lookup and a write, and verifying one costs a hash off a USB disk, so a few dozen marks already
outlast a request. `src/web/photos/jobs.py` is what they share.

**Thumbnails are built on request and cached under `data/photo-thumbs/`.** 76% of the library is HEIC,
which only Safari renders, and an original is a 4032×3024 file on a USB disk. Each conversion takes about
0.3 s, so the cache is what makes the grid usable rather than an optimisation on top of it. Delete it and
the next page rebuilds it. The drive itself still holds nothing but originals.

**A grid row carries no EXIF.** The camera columns are computed from the `exif` blob, and reading 34 MB of
plists off USB to draw a grid costs a page a second. The grid reads named columns only; camera and
exposure load one asset at a time when you open the detail panel.

**A movie's tile is a frame taken from it** with whatever `ffmpeg` is on `PATH`, cached like any other
thumbnail. There is no Python video dependency and nothing to install: without ffmpeg a movie simply has
no tile, which is what every movie showed before.

**Movies play in the detail panel**, straight from the drive's own bytes -- `GET
/api/photos/assets/<id>/video` serves the original with range requests, which is what lets a player seek
and what Safari needs before it will start at all. Nothing is transcoded: re-encoding a 200 MB clip is not
a wait a page can hold. A recent iPhone's `.MOV` is HEVC, so a browser that cannot decode it says so in
the panel rather than showing an empty frame.

**Every tile carries a photo or a play icon.** Playability is decided by suffix rather than by
`item_type`, because Apple files animated GIF, WEBP and AVIF as movies and those are stills as far as a
browser is concerned -- they get the photo icon and the image route, like any other still.

**A tile with no picture says why.** `Hidden in iCloud` for the album this tool excludes on purpose,
`In the trash` for Recently Deleted, `Gone` for something iCloud no longer holds, and `Not backed up` only
for an asset that really is a gap in the backup.

## Limitations

- **Edits are not backed up.** What gets saved is the pre-edit original. Crop or filter something in the
  Photos app and the backup holds what the camera captured, not what you see.
- **HEIC is stored as-is.** 7,300 files. Converting would break both the hash and the "original bytes"
  promise, so finding a decoder in 2040 is your problem.
- **Hidden is never backed up**, so those 92 assets and 2.5 GB cannot be marked `iCloud`: there is no
  copy to fall back on, and the prune refuses them. `Drive` has nothing to delete for them either.
  `Everywhere` will still delete them.
- **There are no tags.** `keywords`, `people`, `title` and `description` come back empty for all 9,711
  assets. Apple's CloudKit does not expose library tags or face data here. This is not a decoding bug: the
  same path produces 6,184 GPS fixes.
- **A prune deletes one asset at a time.** No batching: CloudKit will take a batch, but a partial batch
  failure is far harder to report honestly than a list of per-asset outcomes.
- **`verify` does not refresh metadata.** It reads whatever the last pull left and tells you how old it is.
- **An edit to an old photo will not show up.** Favourite or retitle something from 2018 and the
  incremental pull walks nowhere near it. Empty `album_counts` to force a full pull.
- **The first big download is slow.** Roughly 2 to 5 hours for 25 GB, serial, resumable at any point.
  Steady state after that is a handful of new assets per run.
- **Reaching an old asset is slow.** Albums page newest-first, so something taken today is found in the
  first request and something from years ago means walking most of the library. This is a download
  problem only; a prune fetches by record name and does not care how old an asset is.
- **Only a mark shrinks the drive.** Nothing prunes on its own; the drive grows until you say otherwise.

## What is tested

```bash
uv run --group photos pytest tests/test_backup.py tests/test_library.py tests/test_drive.py \
    tests/test_pull_metadata.py tests/test_icloud_source.py tests/test_migrate_manifest.py \
    tests/test_queries.py tests/test_prune.py tests/test_photos_web.py
```

The decision layer runs on plain values. Fakes are reserved for the two real boundaries, iCloud and the
filesystem.

What is covered on purpose is the risky half: an asset surviving a round trip through the schema
unchanged, an asset that leaves iCloud keeping its row, a short download being discarded, a file at the
wrong size never being overwritten, both halves of a Live Photo staying together, what stays recorded when
a run crashes, exit codes on failure, where the incremental walk stops and what it must not drop when it
does, the manifest import verifying itself, and the CloudKit rank semantics in `test_icloud_source.py`
that nobody would guess (rank 0 is an album's *newest* asset, and ASCENDING reads forward from there).

`icloud_source.py`'s network paths are not covered. They need a live Apple session, and mocking pyicloud
would only test the mock.
