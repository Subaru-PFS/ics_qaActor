# Changelog

All notable changes to `ics_qaActor` are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Releases are the annotated git tags (`0.4.2`, ...);
the notes of those before 0.5.0 are in the tags' messages (`git tag -n1`).

## [Unreleased]

### Added

- **reduceExposure failures are recorded in qadb** (PIPE2D-2036) — when `reduceExposureStatus` reports a
  non-zero returnCode the actor still skips QA, but it now enqueues the failure and the controller runs
  `drpQa.py markReduceFailed`, which writes a `REDUCE FAILED` verdict to qadb for each detector the visit
  exposed (under this actor's collection and a RUN of its own). The dashboard then shows the failure instead
  of leaving the visit awaiting QA. Needs qadb loading on and an opdb (the exposed cameras are read from it),
  and drp_qa with the `markReduceFailed` subcommand; without them the failure is only logged, as before.

## [0.5.0]

### Changed

- **The loader is `drpQa.py load`; needs drp_qa 0.5.0 or later** (INSTRM-3123) — drp_qa 0.5.0 removed
  `bin.src/loadQadb.py` and `bin.src/twilightQa.py`. Each visit's load is now
  `python $DRP_QA_DIR/bin.src/drpQa.py load` with the same options as before (`-b`, `-c`, `--visits`,
  `--mode realtime`, the qadb's `--host`/`--port`/`--user`/`--dbname` and the opdb's `--opdb-*`).
  `engine.qadb.loader` still overrides the path and names `drpQa.py`; the actor adds `load`. An override still
  naming `loadQadb.py` fails every load.
- **Twilight sequences are judged by the load** (INSTRM-3123) — given the opdb, `drpQa.py load` measures and
  judges a twilight sequence once it is complete, so the load of its last visit judges it. The actor runs one
  drp_qa subprocess per visit, not two: the separate judge, its exit statuses and its timeout are gone
  (`TWILIGHT_JUDGE`, `TWILIGHT_TIMEOUT`, the five `TWILIGHT_*` statuses, `twilight_cmd`, `judge_twilight`).
  The judge's verdicts are no longer written to the Butler (`twilightQaMetrics`), and with qadb loading off
  nothing is judged. A WARN or FAIL sequence no longer gets a WARNING line in the actor log: the loader's
  output is relayed at INFO. The judging runs inside the load's timeout (`engine.qadb.timeout`, 300 s).
- **`qa twilight <visit>` loads the visit** (INSTRM-3123) — it keeps its name, and queues a load of the visit
  without its QA, which judges the visit's sequence if complete. It now fails with qadb loading off as well
  as without a `db.opdb`. A visit the actor never processed needs `qa process <visit>` instead. `start` and
  `status` say the load judges twilights, and `qadb:` names `drpQa.py load`.
