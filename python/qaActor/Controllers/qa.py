import logging
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

#: A `$VAR` or `${VAR}` that `os.path.expandvars` left behind, meaning it was unset.
UNEXPANDED_VAR = re.compile(r"\$\{?\w+")

#: Seconds a single `pipetask` run may take before it is killed. Overridable as
#: `engine.timeout` in qa.yaml; set it to 0 or null there to disable the watchdog.
DEFAULT_TIMEOUT = 600

#: Seconds a qadb load may take before it is killed. Overridable as `engine.qadb.timeout`.
DEFAULT_LOAD_TIMEOUT = 300

#: The module that owns the output chain (`qaActor.chain`), run with the stack's python like the loader:
#: at start, and after each visit's pipetask to prepend the RUN it wrote (INSTRM-3096).
CHAIN_MODULE = "qaActor.chain"

#: Seconds defining the chain may take: a Butler import and one registry transaction.
CHAIN_TIMEOUT = 120

#: drp_qa's command, whose `load` subcommand copies a visit's QA numbers from the Butler into qadb and
#: judges them. Given the opdb, it also measures and judges a twilight sequence once the
#: sequence is complete, so the load of its last visit judges it. `engine.qadb.loader` overrides the path;
#: the actor adds the subcommand (`LOAD_SUBCOMMAND`).
DEFAULT_LOADER = "$DRP_QA_DIR/bin.src/drpQa.py"

#: The subcommand of the loader the actor runs.
LOAD_SUBCOMMAND = "load"

#: The loader subcommand that records a visit's reduceExposure failure in qadb (REDUCE FAILED), so the
#: dashboard shows the failure instead of leaving the visit awaiting QA (PIPE2D-2036).
MARK_FAILED_SUBCOMMAND = "markReduceFailed"


@dataclass(frozen=True)
class LoadVisit:
    """A queue item asking for a visit's load alone, without its QA: `qa twilight <visit>`."""

    visit: int


@dataclass(frozen=True)
class DefineChain:
    """A queue item asking for the output chain to be defined (or repaired): put first at start."""


@dataclass(frozen=True)
class MarkFailed:
    """A queue item asking that a visit's reduceExposure failure be recorded in qadb (REDUCE FAILED).

    The `Drp` model enqueues it when ``reduceExposureStatus`` reports a non-zero returnCode, so QA is
    skipped but the dashboard still shows the failure rather than leaving the visit awaiting QA.
    """

    visit: int
    return_code: int | None = None
    status: str = ""


class qa(threading.Thread):  # noqa: N801 — name must match the module for ICC.attachController
    """QA processing loop.

    Runs for the lifetime of the actor: `start` and `stop` are the controller
    lifecycle hooks called by `ICC.attachController` / `ICC.detachController`,
    not user-facing commands.
    """

    def __init__(self, actor, name: str, logLevel: int = logging.DEBUG):
        super().__init__(daemon=True, name=name)
        self.actor = actor

        # A child of the actor's logger, not the actor's logger itself: that one
        # is the shared `actor` logger whose level comes from logging.baseLevel in
        # the config, so setting the level on it here would quietly re-level the
        # whole actor. Records still propagate to the actor's handlers.
        self.logger = actor.logger.getChild(name)
        self.logger.setLevel(logLevel)

        self.logger.info(f"Setting up QA with {name=}")

        cfg = actor.actorConfig["engine"]

        self.datastore = cfg["butler"]["datastore"]

        self.input_collections = cfg["butler"]["input"]
        if isinstance(self.input_collections, list):
            self.input_collections = ",".join(self.input_collections)
        self.chain_timeout = CHAIN_TIMEOUT

        self.output_collection = cfg["butler"]["output"]
        self.pipeline_path = os.path.expandvars(cfg["pipeline"])
        self.num_procs = cfg.get("num_procs", 8)

        # expandvars leaves an unset variable in place rather than raising, so an
        # unset DRP_QA_DIR would otherwise surface only as the same pipetask
        # failure on every visit, for as long as the actor runs. Fail here
        # instead: connectionMade turns a controller that will not build into a
        # loud startup failure.
        unexpanded = UNEXPANDED_VAR.search(self.pipeline_path)
        if unexpanded:
            raise RuntimeError(
                f"pipeline path {cfg['pipeline']!r} still contains {unexpanded.group()!r} "
                f"after expansion — is DRP_QA_DIR set in the actor's environment?"
            )
        self.timeout = cfg.get("timeout", DEFAULT_TIMEOUT)

        # INSTRM-3040: each visit's verdicts are copied into the qadb that the shared config
        # (actors.yaml, merged into this one) names as `db.qadb`; the opdb, `db.opdb`, fills qadb's
        # visit rows. `engine.qadb.load` defaults to on, but an unset one only tries: without a
        # db.qadb nothing is loaded and `start` says so. `qa qadb on|off` switches it at runtime.
        load = cfg.get("qadb") or {}
        databases = actor.actorConfig.get("db") or {}
        self.qadb = databases.get("qadb") or None
        self.opdb = databases.get("opdb") or None
        self.opdb_host = (self.opdb or {}).get("host")
        self.loader_path = os.path.expandvars(load.get("loader", DEFAULT_LOADER))
        self.load_timeout = load.get("timeout", DEFAULT_LOAD_TIMEOUT)
        self.qadb_load = False
        if load.get("load", True):
            try:
                self.set_qadb_load(True)
            except RuntimeError:
                # A missing db.qadb stops the actor only under an explicit `load: true`; the
                # default just doesn't load. An unset loader variable stops it either way.
                if "load" in load or self.qadb is not None:
                    raise

        self.processing_queue = queue.Queue()

        self._current_visit = None

    def set_qadb_load(self, enabled):
        """Switch loading into qadb on or off, from the next visit's load on.

        Raises
        ------
        RuntimeError
            Switching on without a db.qadb in the config, or with a loader path that still
            holds an unset variable; loading is left as it was.
        """
        if enabled:
            if self.qadb is None:
                raise RuntimeError("cannot load into qadb: the config has no db.qadb (actors.yaml)")
            unexpanded = UNEXPANDED_VAR.search(self.loader_path)
            if unexpanded:
                raise RuntimeError(
                    f"qadb loader path {self.loader_path!r} still contains {unexpanded.group()!r} "
                    f"after expansion — is DRP_QA_DIR set in the actor's environment?"
                )
        self.qadb_load = bool(enabled)

    def describe_qadb_load(self):
        """One line on whether, and where, each visit is loaded into qadb."""
        if self.qadb_load:
            return (
                f"qadb: loading each visit to {self.qadb['host']}:{self.qadb.get('port', 5436)} "
                f"as {self.qadb.get('user', 'pfs')}, with {self.loader_path} {LOAD_SUBCOMMAND}"
            )
        if self.qadb is None:
            return "qadb: not loading (the config has no db.qadb)"
        return "qadb: not loading (switched off)"

    def describe_chain(self):
        """One line on the output chain and who maintains it."""
        return (
            f"output: {self.output_collection} = this actor's RUNs, newest first,"
            f" then {self.input_collections} as chained members"
            f" (python -m {CHAIN_MODULE}; pipetask writes a RUN per visit)"
        )

    def describe_twilight(self):
        """One line on whether twilight sequences are judged: by the load of a sequence's last visit."""
        if self.opdb_host is None:
            return "twilight: not judging sequences (the config has no db.opdb)"
        if self.qadb is None:
            return "twilight: not judging sequences (the config has no db.qadb to load into)"
        if not self.qadb_load:
            return (
                "twilight: not judging sequences (qadb loading is off: the load of a sequence's last visit"
                " judges it, and `qa twilight <visit>` loads that visit later)"
            )
        return (
            f"twilight: the load of a sequence's last visit judges it, reading the sequence from"
            f" opdb {self.opdb_host}:{self.opdb.get('port', 5432)} as {self.opdb.get('user', 'public_user')}"
        )

    def opdb_args(self):
        """Return the opdb options the loader takes, from the config's `db.opdb`.

        The loader defaults a missing key the same way (5432, `public_user`, `opdb`); the actor
        spells them out so the logged command is complete.
        """
        # fmt: off
        return [
            "--opdb-host", f"{self.opdb_host}",
            "--opdb-port", f"{self.opdb.get('port', 5432)}",
            "--opdb-user", f"{self.opdb.get('user', 'public_user')}",
            "--opdb-dbname", f"{self.opdb.get('dbname', 'opdb')}",
        ]
        # fmt: on

    @property
    def current_visit(self):
        """The visit being processed right now, or None if the loop is idle."""
        return self._current_visit

    def start(self, cmd=None):
        """Start the QA processing loop."""
        self.logger.info("Starting QA processing loop")
        self.logger.info(f"Pipeline path: {self.pipeline_path}")
        self.logger.info(f"Datastore: {self.datastore}")
        self.logger.info(f"Input collections: {self.input_collections}")
        self.logger.info(self.describe_chain())
        self.logger.info(self.describe_qadb_load())
        self.logger.info(self.describe_twilight())

        # First thing the loop does: define (or repair) the chain, so the loader sees
        # this actor's RUNs behind the current inputs from the first visit on. At the head of the
        # queue, ahead of any visit the drp model enqueued before the controller started.
        with self.processing_queue.mutex:
            self.processing_queue.queue.appendleft(DefineChain())
            self.processing_queue.unfinished_tasks += 1
            self.processing_queue.not_empty.notify()
        super().start()

        if cmd:
            cmd.inform('text="QA processing loop started"')
            cmd.finish()

    def stop(self, cmd=None):
        """Ask the QA processing loop to exit.

        The thread is a daemon, so it also goes away with the actor process; the
        sentinel just lets an idle loop unblock and exit cleanly. This is
        cooperative on purpose — a visit already being processed runs to
        completion rather than being killed mid-pipeline.
        """
        self.logger.info("Stopping QA processing loop")
        self.processing_queue.put(None)

        if cmd:
            cmd.inform('text="QA processing loop stopped"')
            cmd.finish()

    def run(self):
        """Consume visits from the queue and run the QA pipeline on each."""
        while True:
            self.logger.info("Waiting for visits on the QA processing queue")
            # Blocks until a visit is enqueued.
            item = self.processing_queue.get()

            # `stop` enqueues None as the shutdown sentinel.
            if item is None:
                self.logger.info("Received the stop sentinel, exiting QA processing loop")
                break

            if isinstance(item, DefineChain):
                try:
                    self.define_chain()
                except Exception as e:
                    self.logger.warning(f"Error defining the output chain: {e}")
                continue

            if isinstance(item, MarkFailed):
                try:
                    self._current_visit = item.visit
                    self.mark_reduce_failed(item.visit, item.return_code, item.status)
                except Exception as e:
                    self.logger.warning(f"Error marking {item.visit} REDUCE FAILED: {e}")
                finally:
                    self._current_visit = None
                continue

            visit_id = item.visit if isinstance(item, LoadVisit) else item
            try:
                self._current_visit = visit_id
                if isinstance(item, LoadVisit):
                    self.logger.info(f"Loading visit {visit_id} alone, without its QA (qa twilight)")
                    if self.load_cmd(visit_id) is None:
                        # `qa qadb off` since the command was queued.
                        self.logger.warning(f"{visit_id=} not loaded: {self.describe_qadb_load()}")
                    else:
                        self.load_qadb(visit_id)
                    continue
                self.logger.info(f"Processing visit: {visit_id}")
                self.run_pipetask(visit_id)
                # Even after a failed or killed pipetask: the images it did judge are in the
                # Butler, and the loader loads what it finds. With the opdb, it judges a twilight
                # sequence too, once this visit completes it.
                self.load_qadb(visit_id)
            except Exception as e:
                self.logger.warning(f"Error processing {visit_id=}: {e}")
            finally:
                self._current_visit = None

    def new_run(self, visit_id):
        """Name the RUN for one visit's pipetask: the output collection, slash, the UTC time and the visit.

        The visit keeps two visits processed within the same second apart (a quick failure, then the
        next): a RUN is one visit's output, and the chain step must not merge them.
        """
        return f"{self.output_collection}/{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{visit_id}"

    def pipetask_cmd(self, visit_id, run=None):
        """Build the pipetask command line for a single visit, writing into `run` (default: a new one).

        The inputs are given as `-i` with `--output-run` and no `-o`: pipetask then resolves the
        inputs when it runs, so a RUN drpActor prepended to drpActor/reductions since the last visit
        is found. With `-o` it would instead search the output chain as stored, a flattened snapshot
        of the inputs it wrote at the last successful run, and find nothing newer (INSTRM-3096);
        `qaActor.chain` keeps the output chain instead (`define_chain`).
        """
        run = run or self.new_run(visit_id)
        # fmt: off
        return [
            "pipetask",
            "--long-log",
            "--log-level", ".=INFO",
            "run",
            "-j", f"{self.num_procs}",
            "-b", self.datastore,
            "-i", self.input_collections,
            "--output-run", run,
            "-p", self.pipeline_path,
            "-d", f"visit = {visit_id}"
        ]
        # fmt: on

    def chain_cmd(self, run=None):
        """Build the command defining the output chain, prepending `run` when given.

        `python` is the one on PATH, as `pipetask` is: the stack's, which has the Butler; it finds
        `qaActor.chain` because EUPS puts this product's python/ on PYTHONPATH.
        """
        # fmt: off
        cmd = [
            "python", "-m", CHAIN_MODULE,
            "-b", self.datastore,
            "-c", self.output_collection,
            "-i", self.input_collections,
        ]
        # fmt: on
        if run is not None:
            cmd += ["--run", run]
        return cmd

    def define_chain(self, run=None, visit_id=None):
        """Define the output chain, prepending `run` when given, and log the outcome.

        Return whether it succeeded. A failure leaves the visit's QA in its RUN but out of the chain,
        where the loader does not see it; the logged command puts it there by hand.
        """
        cmd = self.chain_cmd(run)

        def watchdog(process, visit_id, timed_out):
            return self._start_watchdog(process, visit_id, timed_out, self.chain_timeout, "output chain")

        returncode, timed_out = self._run_streamed(cmd, visit_id, watchdog)
        # As in `run_pipetask`: a zero return code means it finished, whatever the watchdog says.
        if returncode == 0:
            return True
        where = (
            f"{run} is not in {self.output_collection}" if run else f"{self.output_collection} is not defined"
        )
        self.logger.warning(
            f"output chain {'timed out' if timed_out else f'failed (returncode {returncode})'}:"
            f" {where}, so the loader does not see its QA until this is rerun"
        )
        self.logger.warning(f"Failed command: {' '.join(cmd)}")
        return False

    def run_pipetask(self, visit_id):
        """Run the QA pipeline for a single visit, relaying pipetask output to the log.

        Parameters
        ----------
        visit_id : int
            The visit identifier to process through the QA pipeline.

        Notes
        -----
        - Constructs the pipetask command using `pipetask_cmd()`
        - Redirects stderr to stdout to ensure all pipeline output is captured
        - Streams pipeline output in real-time to the logger at the INFO level
        - Kills the pipeline, and logs it, if it outlives `self.timeout`
        - Logs warnings if the pipeline fails (non-zero return code)
        - Logs a success message when the pipeline completes successfully
        """
        run = self.new_run(visit_id)
        cmd = self.pipetask_cmd(visit_id, run)
        returncode, timed_out = self._run_streamed(cmd, visit_id, self._start_watchdog)

        # `and returncode` guards the narrow race where the watchdog fires just
        # as the pipeline exits cleanly: a killed child reports a signal exit, so
        # a zero return code means it really did finish in time.
        if timed_out and returncode != 0:
            self.logger.warning(f"QA pipetask timed out for {visit_id=} after {self.timeout}s, killed")
            self.logger.warning(f"Timed out command: {' '.join(cmd)}")
        elif returncode != 0:
            self.logger.warning(f"QA pipetask failed for {visit_id=} (returncode {returncode})")
            self.logger.warning(f"Failed command: {' '.join(cmd)}")
        else:
            self.logger.info(f"QA complete for {visit_id=}")
        # Failed or killed too: the images it did judge are in the RUN, and the loader loads what it
        # finds there once the RUN is in the chain (a RUN pipetask never wrote is reported, not chained).
        self.define_chain(run, visit_id)

    def load_cmd(self, visit_id):
        """Build the loader's command line for a single visit; None when loading is off.

        `python` is the one on PATH, as `pipetask` is: the stack's, which the loader needs. The
        loader is `drpQa.py`, and `load` its subcommand (`LOAD_SUBCOMMAND`).
        """
        if not self.qadb_load:
            return None
        # fmt: off
        cmd = [
            "python", self.loader_path, LOAD_SUBCOMMAND,
            "-b", self.datastore,
            "-c", self.output_collection,
            "--visits", f"{visit_id}",
            "--mode", "realtime",
            "--host", f"{self.qadb['host']}",
            "--port", f"{self.qadb.get('port', 5436)}",
            "--user", f"{self.qadb.get('user', 'pfs')}",
            "--dbname", f"{self.qadb.get('dbname', 'qadb')}",
        ]
        # fmt: on
        # The opdb fills qadb's pfs_visit (description, design, issued_at: the dashboard's visit times),
        # and holds a twilight visit's IIC sequence, which the loader judges once it is complete.
        if self.opdb_host:
            cmd += self.opdb_args()
        return cmd

    def load_qadb(self, visit_id):
        """Load a visit's QA numbers into qadb and judge them, if configured; log the outcome.

        The loader is idempotent, so a visit loaded twice (a re-run, `qa process`) is replaced,
        not duplicated. A failure is logged and does not stop the loop: the Butler stays the
        record, and the visit can be loaded again later with the same command.
        """
        cmd = self.load_cmd(visit_id)
        if cmd is None:
            return

        def watchdog(process, visit_id, timed_out):
            return self._start_watchdog(process, visit_id, timed_out, self.load_timeout, "qadb load")

        returncode, timed_out = self._run_streamed(cmd, visit_id, watchdog)
        if timed_out and returncode != 0:
            self.logger.warning(f"qadb load timed out for {visit_id=} after {self.load_timeout}s, killed")
            self.logger.warning(f"Timed out command: {' '.join(cmd)}")
        elif returncode != 0:
            self.logger.warning(f"qadb load failed for {visit_id=} (returncode {returncode})")
            self.logger.warning(f"Failed command: {' '.join(cmd)}")
        else:
            self.logger.info(f"qadb loaded for {visit_id=}")

    def mark_failed_cmd(self, visit_id, return_code=None, status=""):
        """Build the command recording a visit's reduceExposure failure in qadb; None when it cannot run.

        Needs qadb loading on and an opdb (the visit's exposed cameras are read from it, one verdict row
        per detector). The RUN is one of this actor's own (`new_run`), so the marker's processing is kept
        apart from a real reduction's. The loader subcommand is `markReduceFailed` (`MARK_FAILED_SUBCOMMAND`).
        """
        if not self.qadb_load or not self.opdb_host:
            return None
        # fmt: off
        cmd = [
            "python", self.loader_path, MARK_FAILED_SUBCOMMAND,
            "-c", self.output_collection,
            "--run", self.new_run(visit_id),
            "--visit", f"{visit_id}",
            "--host", f"{self.qadb['host']}",
            "--port", f"{self.qadb.get('port', 5436)}",
            "--user", f"{self.qadb.get('user', 'pfs')}",
            "--dbname", f"{self.qadb.get('dbname', 'qadb')}",
        ]
        # fmt: on
        if return_code is not None:
            cmd += ["--return-code", f"{return_code}"]
        if status:
            cmd += ["--status", f"{status}"]
        cmd += self.opdb_args()
        return cmd

    def mark_reduce_failed(self, visit_id, return_code=None, status=""):
        """Record a visit's reduceExposure failure in qadb (REDUCE FAILED), and log the outcome.

        Skipped, with a log line, when qadb loading is off or there is no opdb. Idempotent, and a failure
        is only logged, like `load_qadb`: it must not stop the loop.
        """
        cmd = self.mark_failed_cmd(visit_id, return_code, status)
        if cmd is None:
            self.logger.warning(
                f"Not marking {visit_id} REDUCE FAILED (needs qadb loading on and an opdb): "
                f"{self.describe_qadb_load()}"
            )
            return

        def watchdog(process, visit_id, timed_out):
            return self._start_watchdog(process, visit_id, timed_out, self.load_timeout, "mark reduce-failed")

        returncode, _timed_out = self._run_streamed(cmd, visit_id, watchdog)
        if returncode != 0:
            self.logger.warning(f"Marking {visit_id} REDUCE FAILED failed (returncode {returncode})")
            self.logger.warning(f"Failed command: {' '.join(cmd)}")
        else:
            self.logger.info(f"Marked {visit_id} REDUCE FAILED in qadb")

    def _run_streamed(self, cmd, visit_id, start_watchdog):
        """Run `cmd`, relaying its output to the log, under a watchdog.

        Returns
        -------
        returncode : int
            The child's exit status.
        timed_out : bool
            Whether the watchdog fired (check `returncode` too: see `run_pipetask`).
        """
        self.logger.info(f"Running: {' '.join(cmd)}")

        timed_out = threading.Event()

        # pipetask writes its own log to stderr, so fold it into stdout: a single
        # stream is all that streams reliably from one reader thread, and there is
        # no second pipe left undrained to fill up and block the child.
        #
        # Popen as a context manager so the stdout pipe is closed and the child
        # reaped even if the read loop raises; otherwise the descriptor lives on
        # until the next garbage collection, once per visit, all night.
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            # A hung pipetask would otherwise block this thread — the actor's only
            # QA consumer — forever, with the stop sentinel never read. Kill it
            # from a watchdog rather than timing out the wait: the loop below
            # blocks on reading a silent child, so a deadline on wait() alone
            # would never be reached.
            watchdog = start_watchdog(process, visit_id, timed_out)
            try:
                for line in process.stdout:
                    self.logger.info(line.rstrip())
                process.wait()
            finally:
                if watchdog is not None:
                    watchdog.cancel()

        return process.returncode, timed_out.is_set()

    def _start_watchdog(self, process, visit_id, timed_out, timeout=None, what="QA pipetask"):
        """Arm a timer that kills `process` once `timeout` (default `self.timeout`) has elapsed.

        Returns None when no timeout is configured.
        """
        timeout = self.timeout if timeout is None else timeout
        if not timeout:
            return None

        def kill():
            # The timer can fire in the moment between the pipeline exiting and
            # the `finally` below retiring it, so check before reporting a
            # timeout for a run that actually finished. `Popen.kill` is itself
            # race-safe — CPython polls and suppresses ProcessLookupError — so
            # this guard is about what gets logged, not about the signal.
            if process.poll() is not None:
                return

            timed_out.set()
            self.logger.warning(f"{what} for {visit_id=} exceeded {timeout}s, killing it")
            process.kill()

        watchdog = threading.Timer(timeout, kill)
        watchdog.daemon = True
        watchdog.start()
        return watchdog

    def enqueue_visit(self, visit_id):
        """Enqueue a visit for QA processing (called by the Drp model).

        Parameters
        ----------
        visit_id : int
            The visit identifier to add to the QA processing queue.
        """
        self.processing_queue.put(visit_id)

    def enqueue_load(self, visit_id):
        """Enqueue a visit's load alone, without its QA (`qa twilight <visit>`), behind the visits queued."""
        self.processing_queue.put(LoadVisit(int(visit_id)))

    def queue_size(self):
        """Return the current number of visits waiting in the queue."""
        return self.processing_queue.qsize()
