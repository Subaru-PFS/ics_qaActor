# ics_qaActor - PFS ICS Quality Assurance Actor

## Overview

The `ics_qaActor` is a component of the Instrument Control System (ICS) for the Subaru Prime Focus Spectrograph (PFS).
Its primary role is to monitor the progress of data reduction and provide quality assurance (QA) feedback after each
exposure is reduced by the `drpActor`.

The actor subscribes to the MHS (Messaging Hub System) and listens for `reduceExposureStatus` keys published by the
`drp2` actor instance. When a new reduction is complete, the visit ID is placed on an internal queue and processed by a
background worker thread that runs the `pipetask`-based QA pipeline.

## Architecture

```
drp2      --[reduceExposureStatus]--> qaActor (Drp model callback)
                                          |
                                     queue.Queue
                                          |
                                    qa controller
                                    (daemon thread)
                                          |
                                      pipetask
                                   (drpQA pipeline)
```

- **`main.py`** — `QaActor(ICC)` entry point; wires models and controllers on connect.
- **`models/drp.py`** — `Drp` class; MHS callback that validates incoming keys and enqueues visit IDs.
- **`Controllers/qa.py`** — `qa` controller; a daemon thread whose `run` method is the QA consumer loop, alive for the
  lifetime of the actor. It is started when the controller is attached, builds and runs the `pipetask` command for each
  visit (into a RUN of its own), chains the RUN, and exposes the queue API to the command layer.
- **`chain.py`** — owns the output chain (`qaActor/reductions`): run with the stack's python by the controller, at
  start and after each visit's `pipetask`, see *The output chain*.
- **`Commands/QaCmd.py`** — MHS command handler.

## Prerequisites

| Dependency      | Notes                                                        |
|-----------------|--------------------------------------------------------------|
| `ics_actorkeys` | EUPS package — MHS key definitions                           |
| `tron_actorcore`| EUPS package — `actorcore`/`opscore`; set up transitively by `ics_actorkeys` |
| `pfs_instdata`  | EUPS package — instrument data                               |
| `pfs_utils`     | EUPS package — PFS utilities                                 |
| `DRP_QA_DIR`    | Environment variable pointing to the DRP QA pipeline package |

## Configuration

Deployment-specific settings — repository paths, collections, the pipeline — are read from
`pfs_instdata/config/actors/qa.yaml` via `actor.actorConfig`, never hardcoded. This is what the file ships today:

```yaml
engine:
  butler:
    datastore: /work/datastore          # Butler repository root
    input: # Butler input collections
      - "drpActor/reductions"
      - "PFS/defaults"
    output: qaActor/reductions          # Butler output collection
  pipeline: "$DRP_QA_DIR/pipelines/drpQA.yaml"  # resolved at runtime
```

Two further keys are optional and fall back to defaults in `Controllers/qa.py` when the file omits them, as it
currently does. Set them explicitly if this deployment wants something other than the default:

```yaml
  num_procs: 8                          # pipetask -j; defaults to 8
  timeout: 600                          # seconds before a hung pipetask is killed;
                                        # defaults to 600 (10 min), set to 0 to disable
```

Not everything is configurable: the `pipetask` logging flags (`--long-log`, `--log-level .=INFO`) are fixed in
`pipetask_cmd()`, and so is how the output is collected (see *The output chain*).

A `pipetask` run that outlives `timeout` is killed and logged as a timeout. The QA
consumer is a single thread, so without this one stuck visit would block every
visit behind it for the rest of the night.

The `$DRP_QA_DIR` environment variable must be set and point to the DRP QA pipeline package. The QA
controller refuses to start if it is unset, rather than letting every visit fail inside `pipetask`.

### The output chain

Each visit's `pipetask` writes into a RUN of its own, `qaActor/reductions/<UTC time>`, given as
`--output-run` with the inputs as `-i` and no `-o`. The actor then prepends the RUN to the CHAINED
`qaActor/reductions` itself, with `python -m qaActor.chain` (the stack's python, as for the loader), and
at start it defines, or repairs, that chain as: this actor's RUNs, newest first, then the input
collections as chained members. The loader and the judge read `qaActor/reductions`, so they see every
visit's QA behind the current inputs.

`pipetask` never manages the chain (INSTRM-3096). Given `-o`, it writes the inputs into the chain
*flattened*, as they were at that run, and builds every later quantum graph against the chain as stored;
the drpActor prepends a RUN to `drpActor/reductions` several times a night, so the first new one made
every later visit's graph empty, and `--rebase` cannot help because it rewrites the chain only after a
successful run. The actor was silent from 2026-09-02 until this was found.

A failed `pipetask` is chained too: the images it did judge are in the RUN, and the loader loads what it
finds. A RUN `pipetask` never wrote is reported and left out. If chaining itself fails, the warning names
the RUN and the command to rerun by hand, since until then the loader and the judge do not see that
visit.

### Loading verdicts into qadb

Each visit's QA tables and per-image verdicts are copied into qadb after its `pipetask`
(INSTRM-3040), by drp_qa's loader run the way `pipetask` is:
`python $DRP_QA_DIR/bin.src/loadQadb.py -c <output> --visits <visit> --mode realtime ...`.
`engine.qadb.load` defaults to on; `load: false` switches it off, and `start` and `status` say
whether it is loading. `qa qadb on|off` switches it while the actor runs (see *Commands accepted*).

Where to connect comes from the shared actor config (`pfs_instdata/config/actors/actors.yaml`, merged
into `actorConfig`), as for the other actors' databases: `db.qadb` (`host` required; `port`, `user`,
`dbname` default to 5436, `pfs`, `qadb`) and `db.opdb` (`host` required; `port`, `user`, `dbname`
default to 5432, `public_user`, `opdb`). Both blocks are passed whole to drp_qa's scripts. Only the
switch and its options are in
`qa.yaml`:

```yaml
  qadb:
    load: true               # copy each visit's verdicts into db.qadb; default true
    timeout: 300             # seconds before a hung load is killed; default 300, 0 to disable
    loader: "$DRP_QA_DIR/bin.src/loadQadb.py"   # default
```

- `load: true` set without a `db.qadb` stops the actor at startup, rather than failing every
  visit. With `load` left unset, a missing `db.qadb` only means nothing is loaded.
- The load runs after every `pipetask`, failed or killed ones included: `pipetask` exits non-zero
  when any quantum fails, and the images it did judge are in the Butler. The loader is idempotent,
  so a visit processed again is replaced, not duplicated.
- A failed load is logged and the loop carries on; the Butler stays the record, and the logged
  command loads the visit again by hand.
- The passwords come from the actor user's `~/.pgpass`: one line for `db.qadb`'s account, one for
  `db.opdb`'s (`public_user` by default; reading suffices).
- The opdb fills qadb's visit rows (`pfs_visit`: description, design and `issued_at`, the QA
  dashboard's visit times, PIPE2D-1924); without `db.opdb` they have only the visit id.

### Judging twilight sequences

A twilight sky flat is judged against the other exposures of its IIC sequence, which one visit's
QA cannot see (PIPE2D-1973). So after each visit's load the actor runs drp_qa's judge,
`python $DRP_QA_DIR/bin.src/twilightQa.py -c <output> --sequence-of-visit <visit> --opdb-host <db.opdb.host> --opdb-user ... `
(INSTRM-3089). The judge looks the visit's sequence up in the opdb and does nothing unless it is a
twilight that has finished (`iic_sequence_status`) with a `twilightFiberFlux` stored for every visit
and camera; the call after the sequence's last visit judges it. It prints one verdict per exposure
and arm (`PASS`, `WARN`, `FAIL` or `TOO_FAINT`, with the reasons), stores one verdict per detector
image (`PASS`, `WARN`, `FAIL`, or `UNKNOWN` for the unjudged n arm, with the measurements behind it)
in the Butler as `twilightQaMetrics`, a new RUN prepended to the output collection, then loads the
sequence's visits into qadb: `twilight_qa`, and `qa_gate`, where the sequence check is a third task
beside `imageQualityQa` and `dmLineResiduals` (`spt_qa_database` 0.3.0). Its exit status tells the
actor what happened, and the actor logs it: judged with every exposure `PASS` or `TOO_FAINT`, judged
with a `WARN` or `FAIL` exposure (a WARNING, so it stands out in the log; the judge's output above it
says which exposures and why), pending, not a twilight (quiet), or judged but not stored or loaded, in
which case the judge has printed the verdicts in full, so the actor log holds them. There is no actor
keyword or alert beyond the log yet.

There is nothing to configure: the judge needs `db.opdb` (without it nothing is judged, and
`start` and `status` say so), loads into `db.qadb` while qadb loading is on (with `qadb off` the
verdict is still stored in the Butler, and reaches qadb only through a later `qa twilight`), and has
its own timeout (`TWILIGHT_TIMEOUT`, 300 s: the judging takes seconds, the reload of the sequence's
visits into qadb most of the rest). A sequence the automatic call missed (the actor was down for its
last visit, the store or load failed, loading was off) is judged by hand with `qa twilight <visit>`,
which runs the judge alone, without the visit's QA.

## MHS Interface

### Keys consumed

| Actor  | Key                    | Description                                                 |
|--------|------------------------|-------------------------------------------------------------|
| `drp2` | `reduceExposureStatus` | Signals that a visit has been reduced; carries the visit ID |

Visits whose `reduceExposureStatus` reports a non-zero `returnCode` are logged and skipped — a failed
reduction has nothing worth running QA over.

`drp2` is a numbered `drpActor` instance. `opscore` strips the trailing digits when resolving the keys dictionary, so
the key definitions come from `actorkeys/drp.py` even though the model is registered as `drp2`.

### Commands accepted

| Command              | Description                                                                                             |
|----------------------|---------------------------------------------------------------------------------------------------------|
| `ping`               | Returns the product name; used as a liveness check                                                      |
| `status`             | Reports the visit being processed, the queue depth, whether each visit is loaded into qadb, and whether and where twilight sequences are judged |
| `show`               | Dumps all key-value pairs from all subscribed MHS models                                                |
| `process <visit_id>` | Manually enqueues a visit ID for QA processing (bypasses the automatic `reduceExposureStatus` listener) |
| `qadb on\|off`       | Switches loading into qadb on or off from the next visit's load; fails if `on` has no `db.qadb`         |
| `twilight <visit_id>` | Judges the visit's twilight sequence now, without re-running its QA (see *Judging twilight sequences*); fails without a `db.opdb` |

`qadb on|off` lasts until the controller is rebuilt (an actor restart, or `disconnect`/`connect
controller=qa`); `engine.qadb.load` applies again after that.

The processing loop runs whenever the actor is running; stop or restart the actor itself rather than the loop.

`stop` is cooperative by design: a visit already in `pipetask` runs to completion
rather than being killed mid-pipeline. Only the `timeout` watchdog kills a run.

## License

This project is part of the Subaru Prime Focus Spectrograph (PFS) project and is subject to the licensing terms of the
PFS collaboration.
