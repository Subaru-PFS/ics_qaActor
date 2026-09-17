# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Read [AGENTS.md](AGENTS.md).** It is the canonical working guide for this repo and applies in full
here — do not duplicate its contents into this file. It covers:

- **Development vs production** — everything about venvs, `uv` and `pip` is workstation-only; on
  hardware the actor is set up by EUPS from `ups/ics_qaActor.table`, with no venv involved. The two
  dependency manifests are deliberately different lists.
- **Which interpreter to run** — always `.venv/bin/python`. `.venv/` is this actor's own isolated
  environment, holding only the products it declares, mirroring an EUPS `setup` on real hardware.
- **How to run tests and lint**, including a single test, and how to rebuild the venv from scratch.
- **What is installed and why**, and which sibling actors are deliberately absent.
- **Known environment issues** — the `uv` editable-install bug, and how CI differs.
- **Layout and wiring** — the queue ownership between the `qa` controller and the `Drp` model, and
  the `drp2` model name.
- **Conventions** — the ruff config, the pre-existing lint baseline, and the deliberate MHS-driven
  deviations from PEP 8 naming.
- **Adding a command** — the MHS handler shape to mirror in `Commands/QaCmd.py`.
- **Configuration** — everything comes from `pfs_instdata/config/actors/qa.yaml`; nothing is hardcoded.
- **Things to be careful about** — the controller thread lifecycle, the stderr/stdout fold in
  `run_pipetask`, and the exception handling that keeps the consumer loop alive.

`README.md` documents what the actor does: the MHS interface, the config schema, and the command list.
