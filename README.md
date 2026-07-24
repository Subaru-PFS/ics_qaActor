# ics_qaActor - PFS ICS Quality Assurance Actor

## Overview

The `ics_qaActor` is a component of the Instrument Control System (ICS) for the Subaru Prime Focus Spectrograph (PFS).
Its primary role is to monitor the progress of data reduction and provide quality assurance (QA) feedback after each
exposure is reduced by the `drpActor`.

The actor subscribes to the MHS (Messaging Hub System) and listens for `reduceExposureStatus` keys published by
`drpActor`. When a new reduction is complete, the visit ID is placed on an internal queue and processed by a background
worker that runs the `pipetask`-based QA pipeline.

## Architecture

```
drpActor  --[reduceExposureStatus]--> qaActor (Drp callback)
                                          |
                                    JoinableQueue
                                          |
                                   background Process
                                          |
                                      pipetask
                                   (drpQA pipeline)
```

- **`main.py`** — `QaActor` class; manages the MHS connection, the processing queue, and the worker process.
- **`drp.py`** — `Drp` class; MHS callback that validates incoming keys and enqueues visit IDs.
- **`utils.py`** — `run_qa_for_visit` consumer loop and `run_pipetask` subprocess wrapper.
- **`Commands/QaCmd.py`** — MHS command handler (`ping`, `status`, `show`).

## Prerequisites

| Dependency      | Notes                                                        |
|-----------------|--------------------------------------------------------------|
| `ics_actorkeys` | EUPS package — MHS key definitions                           |
| `pfs_instdata`  | EUPS package — instrument data                               |
| `pfs_utils`     | EUPS package — PFS utilities                                 |
| `DRP_QA_DIR`    | Environment variable pointing to the DRP QA pipeline package |

## Configuration

The following values are currently hardcoded in `main.py` and should be updated to match the active calibration products
before deployment:

| Variable            | Location   | Description                                             |
|---------------------|------------|---------------------------------------------------------|
| `input_collections` | `main.py`  | Butler input collections (calibs, reductions, defaults) |
| `output_collection` | `main.py`  | Butler output collection for QA results                 |
| `/work/datastore`   | `utils.py` | Butler repository (datastore) root path                 |

The QA pipeline is resolved via the `DRP_QA_DIR` environment variable:

```
$DRP_QA_DIR/pipelines/drpQA.yaml#imageQualityQa
```

## MHS Interface

### Keys consumed

| Actor | Key                    | Description                                                 |
|-------|------------------------|-------------------------------------------------------------|
| `drp` | `reduceExposureStatus` | Signals that a visit has been reduced; carries the visit ID |

### Commands accepted

| Command  | Description                                                |
|----------|------------------------------------------------------------|
| `ping`   | Returns the product name; used as a liveness check         |
| `status` | Reports the version key and current processing queue depth |
| `show`   | Dumps all key-value pairs from all subscribed MHS models   |

## License

This project is part of the Subaru Prime Focus Spectrograph (PFS) project and is subject to the licensing terms of the
PFS collaboration.
