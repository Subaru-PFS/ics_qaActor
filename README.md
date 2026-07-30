# ics_qaActor - PFS ICS Quality Assurance Actor

## Overview

The `ics_qaActor` is a component of the Instrument Control System (ICS) for the Subaru Prime Focus Spectrograph (PFS).
Its primary role is to monitor the progress of data reduction and provide quality assurance (QA) feedback after each
exposure is reduced by the `drpActor`.

The actor subscribes to the MHS (Messaging Hub System) and listens for `reduceExposureStatus` keys published by
`drpActor`. When a new reduction is complete, the visit ID is placed on an internal queue and processed by a background
worker thread that runs the `pipetask`-based QA pipeline.

## Architecture

```
drpActor  --[reduceExposureStatus]--> qaActor (Drp model callback)
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
- **`Controllers/qa.py`** — `qa` controller; a daemon thread running the QA processing loop for the lifetime of the
  actor. It is started when the controller is attached and exposes the queue API to the command layer.
- **`Commands/QaCmd.py`** — MHS command handler.
- **`utils.py`** — `run_qa_loop` consumer loop and `run_pipetask` subprocess wrapper.

## Prerequisites

| Dependency      | Notes                                                        |
|-----------------|--------------------------------------------------------------|
| `ics_actorkeys` | EUPS package — MHS key definitions                           |
| `pfs_instdata`  | EUPS package — instrument data                               |
| `pfs_utils`     | EUPS package — PFS utilities                                 |
| `DRP_QA_DIR`    | Environment variable pointing to the DRP QA pipeline package |

## Configuration

All runtime configuration is read from `pfs_instdata/config/actors/qa.yaml` via `actor.actorConfig`. No values are
hardcoded in the source.

```yaml
engine:
  butler:
    datastore: /work/datastore          # Butler repository root
    input: # Butler input collections
      - "PFS/calib/..."
      - "drpActor/reductions"
      - "PFS/defaults"
    output: qaActor/reductions          # Butler output collection
  pipeline: "$DRP_QA_DIR/pipelines/drpQA.yaml"  # resolved at runtime
```

The `$DRP_QA_DIR` environment variable must be set and point to the DRP QA pipeline package.

## MHS Interface

### Keys consumed

| Actor | Key                    | Description                                                 |
|-------|------------------------|-------------------------------------------------------------|
| `drp` | `reduceExposureStatus` | Signals that a visit has been reduced; carries the visit ID |

### Commands accepted

| Command              | Description                                                                                             |
|----------------------|---------------------------------------------------------------------------------------------------------|
| `ping`               | Returns the product name; used as a liveness check                                                      |
| `status`             | Reports the current processing queue depth                                                              |
| `show`               | Dumps all key-value pairs from all subscribed MHS models                                                |
| `process <visit_id>` | Manually enqueues a visit ID for QA processing (bypasses the automatic `reduceExposureStatus` listener) |

The processing loop runs whenever the actor is running; stop or restart the actor itself rather than the loop.

## License

This project is part of the Subaru Prime Focus Spectrograph (PFS) project and is subject to the licensing terms of the
PFS collaboration.
