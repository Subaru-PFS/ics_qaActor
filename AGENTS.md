# AGENTS.md — instructions for AI agents

Working notes for `ics_qaActor`, the PFS ICS quality-assurance actor. Read `README.md` first for
architecture and the MHS interface; this file covers *how to work in the repo*, not what it does.

## Development vs production — read this before trusting anything below

**Everything in this file describes working on the actor at a desk. It is not how the actor runs on
the instrument.** The venv, `uv`, `pip` and the editable installs exist only to make the code
testable on a workstation; none of them exist on the summit.

| | Development (this repo) | Production (hardware) |
|---|---|---|
| Environment | `.venv/`, built with `uv` | EUPS — `setup ics_qaActor` |
| Dependency manifest | `pyproject.toml` | `ups/ics_qaActor.table` |
| How imports resolve | editable installs in `site-packages` | `pathPrepend(PYTHONPATH, ${PRODUCT_DIR}/python)` |
| What runs it | `pytest`, direct calls | `main()` → `QaActor("qa", …)`, launched under MHS/tron |

The EUPS table looks too short until you trace it — `tron_actorcore` and `ics_utils` are never named
by this actor, but arrive transitively:

```
ics_qaActor
├── ics_actorkeys ── tron_actorcore ── ics_config, ics_utils
├── pfs_instdata
├── pfs_utils ─────── (optional) pfs_instdata
└── drp_qa ────────── drp_stella, pfs_utils
```

What follows from this:

- **The two manifests are not the same list, and should not be forced to match.** `ups/*.table` is
  authoritative for production; `pyproject.toml` serves development and CI. `ics_actorkeys` and
  `drp_qa` are EUPS-only products with no pip packaging at all — adding them to `pyproject.toml`
  breaks the CI install outright. Verified 2026-09-16; see *Known environment issues*.
- **Don't "tidy" the EUPS table** by dropping what looks unused, and don't add `tron_actorcore` or
  `ics_utils` to it without reason — the transitive chain above already supplies them.
- **Never let the code depend on a venv.** No `.venv` paths, no `sys.executable` assumptions, no
  pip-installed console scripts: this repo ships no `bin/` and declares no `[project.scripts]`. The
  actor is found by EUPS on `PYTHONPATH` and started by the hub.
- A dependency added for real runtime use belongs in **both** manifests — unless, like the two
  above, it has no pip packaging, in which case the table alone is correct.

## Python interpreter — read this before running anything

*(Development only — see the table above. On hardware there is no venv; EUPS sets `PYTHONPATH`.)*

**Always use `.venv/bin/python`. Never `python`, `python3`, or `pip` off `$PATH`.**

`.venv/` is this actor's **own isolated environment**. It holds only the products
`ups/ics_qaActor.table` and `pyproject.toml` declare, installed editable against the local
checkouts — deliberately mirroring what an EUPS `setup ics_qaActor` puts on `PYTHONPATH` on the
real hardware. Actors are isolated by what they declare, not by sharing one big environment.

`pytest` and `ruff` are installed, so run them directly:

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m pytest --cov --cov-report=term-missing
.venv/bin/ruff check .
.venv/bin/ruff format --diff .
```

Single test: append `::ClassName::test_name` to the file path, or use `-k <expr>`:

```bash
.venv/bin/python -m pytest tests/test_qa_controller.py::TestRunPipetask -q
```

The full suite is 116 tests and should be green. If a test module fails at *collection*, something is
wrong with the environment, not the code — rebuild rather than working around it.

### Do not use bare `uv run` here

`uv run` treats this directory as a *project* and syncs the environment from `pyproject.toml` before
running anything. That is actively destructive here, because `pyproject.toml` declares its siblings
as `git+https://` URLs. `uv sync --dry-run` reports that a sync would:

- replace all five local editable checkouts with pinned git clones, so edits in `ics_utils`,
  `pfs_utils`, `tron_actorcore`, `pfs_instdata` and `datamodel` stop being visible;
- **uninstall `ics-actorkeys`**, which is declared only in `ups/ics_qaActor.table` and not in
  `pyproject.toml` — that breaks the actor outright;
- **uninstall `pytest`, `pytest-cov`, `ruff` and `coverage`**, since dev extras are not synced
  without `--extra dev`, so `uv run pytest` removes pytest and then fails to find it;
- write a `uv.lock` pinning all of the above.

`uv run --no-sync …` skips the sync and is safe — it runs the suite green and leaves the venv
untouched. It is a fine alternative if you prefer it. But `.venv/bin/python` is the documented
default because forgetting `--no-sync` silently dismantles the environment, which is a worse failure
than simply typing a path.

### What is installed, and why

Installed editable from local checkouts, one per product:

| Product | Declared in |
|---|---|
| `pfs_tron_actorcore` | transitive via `ics_utils`; provides `actorcore` / `opscore` |
| `ics_utils` | `pyproject.toml` |
| `pfs_utils` | table + `pyproject.toml` |
| `pfs_instdata` | table + `pyproject.toml` |
| `ics_actorkeys` | `ups/ics_qaActor.table` only — not a `pyproject.toml` dependency |
| `pfs_datamodel` | transitive via `ics_utils` / `pfs_utils` |

Deliberately **absent**: every sibling actor (`ics_fpsActor`, `ics_mcsActor`, …) and the cobra
utilities (`ics_cobraOps`, `ics_cobraCharmer`). This actor imports none of them, and on hardware
they would never be set up for it. Keep it that way — do not install a sibling actor here to make an
import work; if the import is genuinely needed, declare it in the table and `pyproject.toml` first.

`drp_qa` is in the EUPS table but is *not* installed: the actor only needs `$DRP_QA_DIR` as a path
to hand `pipetask`, never as an import.

### Rebuilding the environment

`.venv/` is gitignored and disposable — rebuilding it is a minute's work, so rebuild rather than
patching around a broken environment:

```bash
rm -rf .venv && uv venv --python 3.13 .venv

# One -e per product from the table above, pointing at wherever you cloned it,
# then -e . for this actor.
VIRTUAL_ENV=$PWD/.venv uv pip install --no-deps \
  -e /path/to/tron_actorcore -e /path/to/ics_utils -e /path/to/pfs_utils \
  -e /path/to/pfs_instdata -e /path/to/ics_actorkeys -e /path/to/datamodel -e .

VIRTUAL_ENV=$PWD/.venv uv pip install \
  astroplan astropy astropy_iers_data fitsio future gitpython matplotlib numpy pandas ply \
  'psycopg[binary]' psycopg2-binary pycryptodome pytz scipy sqlalchemy twisted \
  pytest pytest-cov ruff
```

`--no-deps` on the first install is load-bearing: `ics_utils` and `pfs_utils` declare their siblings
as `git+https://` URLs, so without it those repos get cloned from GitHub *over* your local
checkouts. That is why the third-party requirements are listed explicitly in the second install.

Then confirm: `.venv/bin/python -m pytest tests -q` should report 116 passed.

## Known environment issues

Verify current behaviour before trusting these; last confirmed 2026-09-16.

- **`uv` must be 0.12 or newer.** Releases around 0.5.x could not install an editable from a path —
  they treated `-e <path>` as a named requirement and failed with
  `<name> was not found in the package registry`. If you hit that, check `uv --version` before
  believing anything else about the environment.
- **`ics_actorkeys` and `drp_qa` cannot go in `pyproject.toml`.** They are EUPS products with no
  pip packaging — `ics_actorkeys` upstream has neither `setup.py` nor `pyproject.toml`, so adding
  `ics-actorkeys @ git+…` makes `pip install -e ".[dev]"` fail with *"does not appear to be a Python
  project"*. Tried and reverted 2026-09-16. Nothing in the code imports `actorkeys` anyway: the only
  reference is a docstring type in `models/drp.py`, and `QaCmd` builds its `KeysDictionary`
  directly rather than loading one. Production gets it from `ups/ics_qaActor.table`, which is
  correct and sufficient.
- **`eups` is not installed and should not be.** `actorcore/Actor.py` imports it at module scope but
  only uses it inside `Actor.__init__`, which no test calls, so `tests/conftest.py` stubs it. A bare
  `python -c "import actorcore.ICC"` outside pytest will therefore fail on `No module named 'eups'`
  — that is expected, not a broken venv.
- CI (`.github/workflows/tests.yml`) installs with plain `python -m pip install -e ".[dev]"` on 3.12
  and 3.13, resolving the git URLs rather than the local checkouts. It is the reference for a clean
  build on a fresh machine; the local venv is the reference for day-to-day work.

## Layout

```
python/qaActor/
  main.py              QaActor(ICC) — wires models and controllers in connectionMade
  models/drp.py        Drp — MHS callback; validates reduceExposureStatus, enqueues visit IDs
  Controllers/qa.py    qa — daemon thread; the QA consumer loop, builds and runs pipetask
  Commands/QaCmd.py    QaCmd — MHS command handlers
tests/                 pytest suite; conftest.py stubs `eups` so tests run offline
ups/ics_qaActor.table  EUPS dependencies for deployment
```

The single `queue.Queue` is owned by the `qa` controller; `Drp` reaches it through
`actor.controllers["qa"]` on every callback rather than holding a reference, so the controller must
be attached before a key can be processed — that is why `attachAllControllers()` comes first in
`main.py`. Resolving it per call is deliberate: `attachController` builds a *new* controller, with a
new queue, on every hub reconnect, and a captured queue would be orphaned the first time that
happens. The MHS model subscribed to is `drp2`, a numbered `drpActor` instance; `opscore` strips the
trailing digits, so its keys come from `actorkeys/drp.py`.

`version.py` is generated by `lsst-versions` from git tags. Never edit it by hand.

## Conventions

Enforced by ruff (`pyproject.toml`): line length **110**, target **py312**, numpy docstring
convention, isort with `pfs`/`ics`/`qaActor` as first-party. Run `ruff check .` and
`ruff format --diff .` before you claim a change is done.

**`ruff check .` and `ruff format --check .` are clean, and CI enforces both.** The `lint` job in
`.github/workflows/tests.yml` runs them on every push and pull request, so a non-empty report means
you broke something — there is no longer a baseline to compare against.

The old `N806`/`D401` backlog in `tests/` was resolved the way this file prescribed: by exempting
the rules in `per-file-ignores` for `tests/**/*.py`, not by renaming. `N812` joined them for the
same reason. **The test suite's camelCase locals, helpers and import aliases are intentional — don't
rename them to snake_case.** They mirror the actorcore/opscore API the doubles stand in for.

The lint job pins its ruff version rather than installing the `dev` extra, so linting needs none of
the git dependencies and finishes in seconds. Bump that pin and `pyproject.toml` together.

Naming here is deliberately inconsistent with PEP 8 in places, because MHS requires it:

- `class qa` in `Controllers/qa.py` is lowercase to match the module name —
  `ICC.attachController` looks the class up by module name. It carries an explicit
  `# noqa: N801`. Don't "fix" it.
- **Names imposed by upstream keep their camelCase** — overrides and attributes whose spelling
  `actorcore`/`opscore` dictates, such as `connectionMade`, `connectionLost`, `keyVarDict` and
  `actorConfig`. `N802`/`N803` are globally ignored for this reason. The test of whether a name
  belongs here is whether something outside this repo chose it, not whether MHS calls it.
- **Names we choose are snake_case, even when MHS calls them.**
  `Drp.check_reduced_exposure_status` is ours — we register it with `addCallback`, so nothing
  upstream constrains the spelling. Same for `enqueue_visit`, `queue_size`, `run_pipetask` and
  `current_visit`.
- Test fixtures and helpers use camelCase (`actorConfig`, `drpQaDir`, `processingQueue`,
  `previousLevel`) to match the actor API they stand in for. Follow the surrounding style.

## Adding a command

`Commands/QaCmd.py` follows the standard MHS handler shape — mirror it exactly:

1. Add a `(name, argSpec, handler)` tuple to `self.vocab` in `__init__`.
2. If the command takes arguments, add a `keys.Key(...)` to the `KeysDictionary` and **bump the
   version tuple** (currently `(1, 1)`) when the dictionary changes.
3. The handler takes `(self, cmd)`, reads arguments via `cmd.cmd.keywords["name"].values[0]`,
   reports with `cmd.inform('text="..."')` / `cmd.warn(...)`, and **must** end in `cmd.finish()` on
   every path — an unfinished command hangs the caller.
4. Reach the controller through `self._get_controller()`, not `self.actor.controllers["qa"]`
   directly.
5. Add a test in `tests/test_qa_cmd.py`. Those tests run against the *real* `qa` controller, not a
   mock, so the command/controller seam is covered too.

## Configuration

All runtime config comes from `pfs_instdata/config/actors/qa.yaml` via `actor.actorConfig`. **Do not
hardcode paths, collection names, or process counts** — read them from config, as
`Controllers/qa.py:__init__` does. `cfg["pipeline"]` is passed through `os.path.expandvars`, so
`$DRP_QA_DIR` resolves at runtime; keep that indirection.

`engine.timeout` bounds a single `pipetask` run (default `DEFAULT_TIMEOUT`, 600s; `0` disables it).
It is enforced by a watchdog that kills the child rather than by a deadline on `wait()`: the output
loop blocks reading a silent child, so a `wait()` timeout would never be reached.

## Things to be careful about

- **`connectionMade` runs again on every hub reconnect.** Twisted drives it through a
  `ReconnectingClientFactory`, so everything in it must be safe to repeat. Two things depend on
  that: the `Drp` instance is reused (`addCallback` dedupes by identity, so a fresh one per
  reconnect would stack up a live callback each time), and it resolves the controller queue lazily.
  Don't capture the queue, and don't rebuild the model unconditionally.
- **The controller thread runs for the lifetime of the actor.** `start`/`stop` on `qa` are
  `ICC.attachController`/`detachController` lifecycle hooks, not user commands. `stop` enqueues
  `None` as a shutdown sentinel; the `run` loop must keep honouring it. `stop` is cooperative on
  purpose — `detachController` calls it on every reconnect, so killing a run in flight there would
  abort legitimate QA. Only the `engine.timeout` watchdog kills `pipetask`.
- **The controller logs to `actor.logger.getChild(name)`, not `actor.logger`.** The latter is the
  shared `actor` logger levelled from `logging.baseLevel`; levelling it here would re-level the
  whole actor.
- **`run_pipetask` folds stderr into stdout on purpose.** `pipetask` logs to stderr, and a second
  undrained pipe would fill and block the child. Don't split them back out.
- **Exceptions in `run` are caught and logged, never raised.** One bad visit must not kill the
  consumer loop. Preserve that, and keep `self._current_visit` reset in the `finally`.
- Don't run `pipetask` from an agent session — it needs a real Butler datastore and takes a long
  time. Test the command construction (`pipetask_cmd`) instead, as `tests/test_qa_controller.py`
  does.
- Don't commit, push, or tag unless asked.
