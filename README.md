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
  visit, and exposes the queue API to the command layer.
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
`pipetask_cmd()`.

A `pipetask` run that outlives `timeout` is killed and logged as a timeout. The QA
consumer is a single thread, so without this one stuck visit would block every
visit behind it for the rest of the night.

The `$DRP_QA_DIR` environment variable must be set and point to the DRP QA pipeline package. The QA
controller refuses to start if it is unset, rather than letting every visit fail inside `pipetask`.

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
| `status`             | Reports the current processing queue depth                                                              |
| `show`               | Dumps all key-value pairs from all subscribed MHS models                                                |
| `process <visit_id>` | Manually enqueues a visit ID for QA processing (bypasses the automatic `reduceExposureStatus` listener) |

The processing loop runs whenever the actor is running; stop or restart the actor itself rather than the loop.

`stop` is cooperative by design: a visit already in `pipetask` runs to completion
rather than being killed mid-pipeline. Only the `timeout` watchdog kills a run.

## License

This project is part of the Subaru Prime Focus Spectrograph (PFS) project and is subject to the licensing terms of the
PFS collaboration.
