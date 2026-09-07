# my-cloud

[![CI](https://github.com/momonala/find-my/actions/workflows/ci.yml/badge.svg)](https://github.com/momonala/find-my/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/momonala/find-my/branch/main/graph/badge.svg)](https://codecov.io/gh/momonala/find-my)

Personal iCloud tools sharing one Apple account and one web app.

**[Find My](docs/find-my.md)** locates your iCloud devices and AirTags, from the terminal or from a map
in the browser, and tells you how far each one is from home. It polls in the background and can message
you on Telegram when something moves.

**[Photos](icloud_photo_metadata/README.md)** copies your iCloud photo library onto an external drive so
you can delete the originals from iCloud and get the storage back. What it saved is then browsable and
filterable in the same web app.

They share a repo because they need the same scarce thing: an Apple session that has already been through
two-factor auth. Sign in once with `mycloud devices` and the photo tool reuses that session rather than
prompting again. Past that they share the web shell and almost nothing else.

## Quick start

```bash
uv sync
cp .env.example .env          # then put your Apple ID in it

uv run mycloud all             # devices and trackers, one table
uv run mycloud serve           # web UI on http://127.0.0.1:5016
```

The first command that talks to Apple asks for a 2FA code and caches the session in `.icloud_session/`.
Do that at a terminal before starting the server, because `serve` cannot prompt you and will just come up
empty.

**One session, two writers, one lock.** The poller inside `serve` and the photo tool both read and write
`.icloud_session/`, and pyicloud persists it with a plain truncating `open(path, "w")` on every request
that carries new session headers. Nothing in pyicloud serialises that, so a poll overlapping a photo
command leaves a reader looking at a half-written file: the JSON fails to parse, pyicloud decides there is
no session, and asks for a 2FA code for one that was perfectly good. Measured on a file that size, 192 of
200 concurrent reads came back truncated. `src/core/icloud_session.py` puts an advisory file lock around
every read and every write, and both entry points call it before building a session.

For the photo tool, which has its own dependencies and its own entry point:

```bash
uv sync --group photos
uv run --group photos python -m icloud_photo_metadata plan
```

## Requirements

- Python 3.13. Pinned as `>=3.13,<3.14` in `pyproject.toml` because `cryptography` has no 3.14 wheel yet.
- [uv](https://github.com/astral-sh/uv).
- macOS, but only for AirTags. Their decryption keys come from this Mac's own Find My data, so that one
  command cannot run anywhere else. Everything else is portable.

## Configuration

Two places, split by whether the value is a secret.

**`pyproject.toml`, under `[tool.config]`.** Version controlled. Home coordinates, the port, the Spyglass
host. Five decimal places of latitude is about a metre, which is as precise as any of this needs to be.

```toml
[tool.config]
home_latitude = 52.49890
home_longitude = 13.40350
```

Read it back with `uv run config --all`, or one key at a time with `uv run config --home-latitude`.

**`.env`, git-ignored.** Apple credentials, optional map keys, the Telegram token, and `API_WRITE_TOKEN`.
Copy `.env.example` and fill it in.

```
ICLOUD_USERNAME=you@example.com
ICLOUD_PASSWORD='your-password'
```

Quote the password. `python-dotenv` reads an unquoted `#` as the start of a comment and will silently cut
your password short at that character.

Write routes are unauthenticated by default, which is fine on the localhost address `serve` binds to. Set
`API_WRITE_TOKEN` before putting the app on a network or behind a tunnel; writes then need a matching
`X-Api-Token` header and reads stay open.

## Where things are stored

| Path | Holds | Notes |
|------|-------|-------|
| `data/findmy.db` | Device history, alerts, tracker key alignment | Git-ignored. Schema owned by Alembic |
| `data/google_tiles/` | Cached Google basemap tiles | Pruned at 30 days on boot |
| `data/photo-thumbs/` | JPEGs made from photo originals on demand | Safe to delete, rebuilds itself |
| `.icloud_session/` | Apple cookies, tracker keys | Git-ignored. Shared by both tools |
| `<external drive>/library.db` | The photo library's own record | Travels with the media it describes |

## The web app

`uv run mycloud serve` runs a Flask app that is a shell plus one blueprint per feature. The shell owns the
nav bar, the landing page, the error banners, and the design tokens. A feature owns its page, its JSON
routes under `/api/<feature>/`, and its own CSS and JS. Nothing in a feature folder is reachable from
another feature, which is what stops two pages colliding.

Reads never make a live Apple call. A background poller writes to SQLite once a minute and every read
route just queries that file, so a request costs a database read rather than a multi-second round trip to
Apple.

```
GET /                    landing page, one card per entry in src/web/nav.py
GET /findmy              device map and history
GET /photos             photo gallery, when the backup drive is mounted
GET /api/config          basemap keys, shared by any page that draws a map
GET /api/tiles/google/…  proxied Google basemap tiles
```

### Adding a page

1. Make `src/web/<feature>/` with a `pages.py` blueprint, giving it its own `url_prefix`,
   `template_folder` and `static_folder`. Add an `api.py` under `/api/<feature>` if it needs one.
2. Write a template that extends `base.html` and fills the `title`, `head`, `content` and `scripts`
   blocks. Pass `active_page=<slug>` from the view so the nav highlights it.
3. Add the page to `src/web/nav.py` and its icon to `src/web/templates/_icons.html`.
4. Register both blueprints in `src/web/app.py`.

You get the nav, the banner stack, `tokens.css`, `shell.css` and the shared JS modules for free. Anything
only your page uses goes in your page's folder. That line is the whole point of the split.

## Project structure

The dependency arrow only points inward. `web/` may import `findmy/` and `core/`, `findmy/` may import
`core/`, and `core/` imports neither.

```
my-cloud/
├── src/
│   ├── cli.py                  the `mycloud` command
│   ├── core/                   infrastructure with no domain knowledge
│   │   ├── paths.py            repo root, data/, .icloud_session/
│   │   ├── icloud_session.py   the lock that keeps two processes off one session file
│   │   ├── config.py           non-secret config from pyproject.toml
│   │   ├── env.py              secrets from .env
│   │   ├── db.py               opening and migrating data/findmy.db, no queries
│   │   ├── errors.py           exceptions the fetch layer raises
│   │   └── telemetry.py        Spyglass logging and metrics
│   ├── findmy/                 device and tracker tracking
│   ├── maps/google_tiles.py    session-minted, disk-cached Google basemaps
│   └── web/
│       ├── app.py              app factory, shell routes, blueprint registration
│       ├── nav.py              the page registry
│       ├── auth.py             the write token every feature shares
│       ├── templates/          base.html, _icons.html
│       ├── static/             tokens.css, shell.css, shared JS modules
│       ├── findmy/             the Find My page and its routes
│       └── photos/             the photo gallery and its routes
├── icloud_photo_metadata/      the photo backup tool, own README and dep group
├── migrations/                 Alembic migrations for data/findmy.db
├── tests/
└── install/, deploy.py         systemd units and the deploy CLI
```

## Schema migrations

`data/findmy.db` is migrated by [Alembic](https://alembic.sqlalchemy.org/), never by hand-written DDL in
the app. `init_db()` runs `alembic upgrade head` every time `serve` or `poll` boots, so pulling code and
restarting the service is enough to pick up a new migration. Running it against a database already at head
does nothing, which is what makes it safe on every boot including a crash loop.

To add one:

```bash
uv run alembic revision -m "add whatever column"
# hand-write upgrade() using op.execute("...") with raw SQL
uv run alembic upgrade head
```

There are no SQLAlchemy models in this project, so `--autogenerate` has nothing to compare against and
will produce an empty migration.

The photo library on the external drive does not use Alembic. It is a separate database with a separate
lifetime, and its README explains why it needs no migration system at all.

## Observability

Both processes ship logs and metrics to a [Spyglass](https://github.com/momonala/spyglass) server, wired up
in `src/core/telemetry.py`. Importing that module calls `spyglass.initialize()` once, which attaches a
log-shipping handler to the root logger and creates the shared `metrics` collector. Every other module gets
remote logging through propagation and imports `metrics` when it wants to count something.

Do not call `initialize()` twice in one process. It is not idempotent and you will end up shipping every
log line twice.

## Development

```bash
./test-and-lint.sh    # pytest, black --check, ruff check
```

Tests run against temp SQLite files and Flask's test client, never the network. What is deliberately not
covered is anything needing a live Apple session: `fetch_devices`, `fetch_airtags`, the poller that calls
them, and the iCloud paths in the photo tool. Mocking those would only test the mock.
