# icloud_photo_metadata

Pulls iCloud Photos metadata into a database on an external drive, then backs the whole media library
up beside it so the originals can be deleted from iCloud to reclaim quota. Photos, videos, GIFs — one
kind of thing, one pipeline.

The backup is one-way and additive. Delete an asset from iCloud and its local copy stays, keeping its
row: `in_icloud` goes to 0 and the drive becomes its only copy. Nothing here ever removes anything.

This is a local analysis and backup tool, not part of the `findmy` service. It never runs on the
deploy target, which is why its dependencies live in a `photos` group that plain `uv sync` skips. It
borrows the main app's Apple credentials and session cookie jar, so a session already trusted by
`uv run mycloud devices` needs no second 2FA prompt.

Last Updated: 2026-09-04

## Quick start

```bash
uv sync --group photos                                    # tqdm, excluded by default

# What would a download fetch? Counts, sizes, and where the drive stands
uv run --group photos python -m icloud_photo_metadata plan

# Fetch it
uv run --group photos python -m icloud_photo_metadata download

# Confirm the backup is intact before deleting anything from iCloud
uv run --group photos python -m icloud_photo_metadata verify

# Once only, on a drive that predates the database
uv run --group photos python -m icloud_photo_metadata import-manifest
```

Three commands, no options. `plan` and `download` do the same work and differ only in whether they
execute it, so the plan you read is the one that runs. Both refresh the metadata from iCloud first, so
neither ever reports on a stale view; that refresh is incremental, reading the front of the library
until pages stop carrying new assets, so it costs 6 pages / ~25s rather than the ~99 pages / ~5min a
full pull takes.

Figures below come from one run against a library of **9,711 assets / 26.6 GB of originals**
(September 2026), of which 9,618 are eligible; a full crawl yields 9,618 Library rows against the
9,620 iCloud reports. The drive holds 9,619 assets in 9,750 files.

`plan` is the only one that writes nothing at all.

## Commands

| Command | Does |
|---------|------|
| `plan` | Refresh metadata, then report what a download would fetch: counts, sizes, and how the drive compares to iCloud |
| `download` | The same, then fetch whatever the drive is missing |
| `verify` | Every recorded file present, right size, right bytes |
| `import-manifest` | Move a pre-database `.backup-manifest.json` into the library. One time only |

Nothing is configurable. The backup directory is `DEFAULT_DIR` in `backup.py`, the metadata is always
refreshed before planning, and `verify` always hashes — a flag on any of those would only be a way to
get a different answer to the same question. To force a full metadata pull instead of an incremental
one, empty the `album_counts` table; the next run has no baseline to compare against and reads every
page.

`verify` exits non-zero only on damage to the backup itself. Assets not yet backed up are reported for
information — `download` is what answers that.

## How it works

`download` runs three stages. Only the last one writes anything.

```
① refresh   iCloud ──► 6 pages, ~3s each ──► upsert assets, in one transaction
                       anything an exhaustive crawl missed ──► in_icloud = 0, row kept
                       any page failed? nothing is written, exit 1
                       an album moved further than the merge did? all ~99 pages instead

② index     assets where in_icloud ──► eligible  (album ∉ {Hidden, Recently Deleted})
              every file recorded for this id present and the right size? ──► recorded
              otherwise                                                   ──► missing
            a file no row claims                                          ──► stray, reported

③ apply     missing ──► download, verify length, hash, stamp mtime, record
                        one commit per asset
                        10 failures in a row ──► stop, keep what landed, exit 1
```

**`files` answers "is this backed up?" by asset id, so there is no guessing at any point.** That holds
because the backup directory contains only files this tool downloaded, one per asset, under a name
carrying that asset's id. The drive's own AirDrops and screen recordings live elsewhere and are not
this tool's business.

## Storage

One database, on the drive beside the media it describes. 53.5 MB at 9,711 assets.

| Table | Holds | Lifecycle |
|-------|-------|-----------|
| `assets` | One row per asset iCloud has ever shown us, `in_icloud` saying whether it still does | Durable. Once an asset is freed from iCloud this row is the only thing describing it |
| `asset_resources` | Per-variant size, dimensions and Apple checksum — original, medium, thumb, the video halves | Derived from a pull |
| `files` | Asset id + kind → drive-relative path, size, sha256 | Durable. What `verify` checks and what says an asset is backed up |
| `marks` | Assets flagged for deletion from iCloud | Yours. The only rows nothing can rebuild |
| `album_counts` | The album lengths iCloud reported at the last pull | The incremental pull's baseline. Empty it and the next pull is a full one |
| `meta` | `pulled_at` | Derived |

`camera_make`, `camera_model`, `lens_model`, `iso`, `f_number` and `focal_length` are generated
columns over the `exif` JSON, so filtering on them needs no re-pull — a new one is a schema change
alone. Indexed where it pays: the camera-model facet is 0.9 ms indexed against 88 ms without.

The media is filed by capture year:

```
icloud-backup/
├── library.db
├── 2016/ … 2024/ (2,145)  2025/ (1,449)  2026/ (1,057)
└── IMG_1972~a1b2c3d4.HEIC + IMG_1972~a1b2c3d4.MOV   ← a Live Photo, one asset, two files
```

Credentials come from `.env` at the repo root (`ICLOUD_USERNAME`, `ICLOUD_PASSWORD`); the session
cookie jar is `.icloud_session/`, shared with the main app.

## Project structure

```
icloud_photo_metadata/
├── cli.py              # the three commands and everything that prints
├── __main__.py         # so `python -m icloud_photo_metadata` is the one entry point
├── backup.py           # the pipeline: index → plan → apply. Prints only download progress
├── library.py          # the drive's database: schema, connection, every query
├── drive.py            # list, stat, hash. Nothing else touches the filesystem
├── icloud_source.py    # the only module that knows pyicloud and CloudKit
├── pull_metadata.py    # refresh_incremental() and refresh_full(), called by cli.py
├── models.py           # PhotoMetadata and friends: what an asset is, independent of storage
└── migrate_manifest.py # one-time JSON → database import. Delete once no drive needs it
```

Pure and testable: `eligible`, `build_index`, `build_plan`, `backup_filename` — all four take plain
values, so the tests that cover them touch no database. All I/O is in `drive.py`, `library.py` and
`icloud_source.py`; the only mutation of the drive's files is `apply_plan`. `cli.py` owns every
`console.print`; `apply_plan` reports what happened on its `Outcome` and writes only the progress bar,
so the pipeline can be tested on its return values.

## Key decisions

**Photos and videos are one thing.** Both are assets with original bytes worth keeping, and an
earlier `item_type == "movie"` filter was silently excluding the library's 8 GIFs, 2 MP4s, a WEBP
and an AVIF from the backup. Dropping it deleted a config record, a CLI argument and every
`if kind ==` branch that would have followed.

**The backup directory holds only what this tool put there.** This is the invariant the design rests
on. It bought the deletion of content matching (`matching.py`), redundancy detection (`dedupe.py`) and
every ffprobe call — roughly 500 lines whose only job was inferring what an unknown file was. A file
here that no record claims is now an anomaly a run reports, not a category it tallies. The drive's
ad-hoc AirDrops and screen recordings stay in their own directory, untouched.

**The library is keyed by asset id, and Apple's UUID is that id.** Keying by id rather than path means
a rename cannot break the link. A content hash would not do the job: it does not exist until after the
file is downloaded, so it cannot key an asset that is still only metadata, and a Live Photo is one
asset with two hashes. `sha256` is a column, which is what `verify` reads. A record is only ever
written by the run that put the bytes on the drive, so `recorded_as` distinguishes the two ways that
happens: `downloaded`, or `found on the drive` when a previous run was interrupted between writing a
file and recording it.

**One commit per asset.** The JSON manifest carried each asset's full metadata, so every save rewrote
all 63 MB of it; recording each of 9,617 downloads that way writes a triangular number of bytes —
**418 GB to record 25 GB** — which is why it batched every 100 records and carried the bookkeeping to
make batching safe. A row insert costs the same whether the library holds ten assets or ten thousand
(69 ms per commit, measured on this exFAT drive), so the batching, the flush-on-exit and the "at most
100 records lost on a crash" caveat all go.

**Filed by capture year, under `IMG_4030~a1b2c3d4.HEIC`.** The capture date never changes and keeps
the largest directory near two thousand files. The id fragment is unconditional because iPhone
recycles `IMG_1234` numbering after a reset — 1,324 name collisions in this library, so the common
case — and carrying the id makes names unique with no collision search. `verify` and `download` care
only about the relative paths in `files`, so the layout costs them nothing.

**A pull never deletes an asset row; it clears `in_icloud`.** Freeing quota is the point of the tool,
so an asset leaving iCloud is the expected end of its life here rather than a record to forget — and
once it has left, its row is the only thing that still describes it. The whole pull is one
transaction, so a partial one leaves the library exactly as it was, and a run aborts rather than plan
against a pull that lost pages, since those could hide whole assets. Every pull still overwrites what
it does read, which is what keeps `source_album` honest: an append-only pull skipped ids it already
had and froze each record's album at first sight.

**The default pull reads the front of the library, not all of it.** Every page costs the same ~3s —
the cost is Apple's round trip, not the payload — so page count is the only lever, and rank 0 is an
album's newest asset. The walk reads Library pages until two in a row carry no id the cache has never
seen, then stops: ~4 pages instead of ~99. Hidden and Recently Deleted are re-crawled end to end
regardless, because they are one page between them and being certain of their membership is what
keeps `source_album` honest across a move.

**The incremental pull checks itself against how far each album moved.** What the walk cannot see is
a change deep in the library: an edit to an old asset, or an import whose capture date sorts it past
the pages read. So every pull records the album lengths iCloud reported in `album_counts`, and
the next one escalates to a full pull when an album moved by more than the merge accounts for. The
check compares deltas rather than absolutes — `_unexplained_drift` in `pull_metadata.py` documents why
a reported length can never be checked against a record count. Two changes that cancel out — a
deletion and an old-dated import between two runs — still slip through; emptying `album_counts` forces
the full pull that catches them.

**Two atomicity boundaries.** A pull is one transaction, all-or-nothing; each downloaded file is
temp-then-rename, so no truncated file ever looks complete. The database's own rollback journal
covers the rest, which is what retired the third boundary the two JSON stores each needed.

**Hashing on write is unconditional, and `verify` always checks it.** A size check cannot catch a file
that rots without changing size.

**A run of failures re-opens the session.** Ten consecutive download failures almost always mean one
dead session rather than ten bad assets, so a download re-authenticates and resumes with whatever it
still owes — an overnight run survives however many expiries it takes. Each asset is retried twice at
most, so one asset failing for its own reasons cannot stall every pass, and it gives up only
once three fresh sessions in a row have downloaded nothing. What landed stays recorded either way.

**Everything runs one request at a time.** `_SESSION_LOCK` serializes every call through pyicloud's
session, so the metadata pull's `workers` buys nothing: 4 pages take 12.5s at 1, 4 or 8 workers alike.
Downloads are serial for the same reason plus one of their own — Apple paces the account, not the
connection, and a pool made every dropped connection everyone's problem.

**A page read is retried three times.** Apple resets long-lived connections mid-crawl. Losing the
page that way costs one more request; letting it escape used to cost the whole walk. If the crawl
still dies, it reads as a stalled pass: re-authenticate and resume from the last page bookmark.

**Requests through pyicloud's session are serialized.** Every request rewrites the cookie jar and
the session JSON with a truncating `open()`, so concurrent page reads could otherwise
leave the very session file a download depends on half-written. `_SESSION_LOCK` covers that.

**The progress bar counts pages as well as files.** The crawl walks newest-first, so a backup whose
gap is the *oldest* assets pages past everything it already has before yielding the first file to
fetch — 54 pages of silence, on this library. The bar's `pages=` postfix is what says the crawl
is alive; the file count only moves when a download is recorded. A pass that re-opens the session
resumes from the offset it stopped at rather than re-walking, and does one full sweep at the end
before calling any asset missing from iCloud.

**Nothing deletes.** A file at a backup's path with the wrong size is reported as damage, never
overwritten. Files belonging to no iCloud asset are left entirely alone.

**`icloud_source.py` quarantines pyicloud.** Reading this metadata means leaning on private methods,
raw CKRecord fields, and the `mediaMetaDataEnc` EXIF blob pyicloud never decodes. A patch release can
move any of it; the rest of the tool sees `PhotoMetadata` and nothing else.

**SQLite, not JSON.** The two JSON stores were read-entirely, write-entirely, and for a tool that only
ever asked "what do I still owe?" that was the right shape. Wanting to browse and filter the library —
by capture date, camera, duration, size, location — is the query workload that assumption excluded, so
the decision inverts on its own terms. It pays for itself elsewhere too: a grid page over 9,711 assets
is 0.6 ms, loading every asset is faster than parsing the JSONL was (0.9 s against 1.4 s), and the
database is smaller than the two files it replaced (53.5 MB against 63 + 43 MB) because the manifest's
per-record metadata snapshot is no longer a second copy of the cache.

**Rollback journalling, not WAL.** The drive is exFAT and removable, and WAL would strand `-wal` and
`-shm` files beside the media if it were unplugged mid-write. There is exactly one writer, so WAL's
concurrency buys nothing.

**No migration system.** `assets` and `asset_resources` are derived and could always be re-pulled in
~80s, so a schema change is a rebuild rather than a migration. `PRAGMA user_version` refuses to open a
library a different version wrote, which is the only guard the durable tables (`files`, `marks`) need
until one of them actually changes shape.

## Limitations

- **Edits are not backed up.** `original` is the pre-edit file. Crop or filter something in the Photos
  app and the backup holds what the camera captured, not what you see.
- **HEIC is stored as-is.** 7,300 of these are HEIC, and converting would break both the hash and the
  "original bytes" contract. A decoder is your problem in 2040.
- **`verify` does not refresh the metadata.** It reads whatever the last pull left, and reports its age.
- **An edit to an old photo will not show up.** Favourite, retitle or re-tag something from 2018 and
  the incremental pull walks nowhere near it. Empty `album_counts` to force a full pull.
- **Hidden is never backed up**, so its 92 assets / 2.5 GB cannot safely be deleted from iCloud.
- **There are no tags.** `keywords`, `people`, `title` and `description` come back empty for all 9,711
  assets — Apple's CloudKit does not expose library tags or faces here. The XMP decode path itself
  works, which is where the 6,184 GPS fixes come from.
- **`marks` is written by nothing yet.** The table exists and persists; acting on it is a later job.
- **Downloads are serial.** ~2–5 hours for the initial 25 GB, resumable at any point. Steady state is
  a handful of new assets per run.
- **Downloading an old asset is slow.** Albums page newest-first, so an asset just taken is found in
  the first request, but reaching a years-old one walks most of the library.
- **No retention policy.** The drive only grows; nothing prunes it.

## Development

```bash
uv run --group photos pytest tests/test_backup.py tests/test_library.py tests/test_drive.py \
    tests/test_pull_metadata.py tests/test_icloud_source.py tests/test_migrate_manifest.py
```

Tests drive the whole decision layer with plain values and reserve fakes for the two real boundaries:
iCloud and the filesystem. What is deliberately covered is the risky half — an asset surviving a round
trip through the schema unchanged, an asset that leaves iCloud keeping its row, a short download being
discarded, a file at the wrong size never being overwritten, both halves of a Live Photo, what stays
recorded when a run crashes, exit codes on failure, where the incremental walk stops and what it must
not drop when it does, the manifest import verifying itself, and the counter-intuitive CloudKit rank
semantics in `tests/test_icloud_source.py` (rank 0 is an album's *newest* asset, and ASCENDING reads
forward from it).

`icloud_source.py`'s network paths are not covered — they need a live Apple session, and mocking
pyicloud would only test the mock.
