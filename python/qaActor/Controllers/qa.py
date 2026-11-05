import logging
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass

#: A `$VAR` or `${VAR}` that `os.path.expandvars` left behind, meaning it was unset.
UNEXPANDED_VAR = re.compile(r"\$\{?\w+")

#: Seconds a single `pipetask` run may take before it is killed. Overridable as
#: `engine.timeout` in qa.yaml; set it to 0 or null there to disable the watchdog.
DEFAULT_TIMEOUT = 600

#: Seconds a qadb load may take before it is killed. Overridable as `engine.qadb.timeout`.
DEFAULT_LOAD_TIMEOUT = 300

#: drp_qa's loader, which copies a visit's QA tables from the Butler into qadb (PIPE2D-1385).
DEFAULT_LOADER = "$DRP_QA_DIR/bin.src/loadQadb.py"

#: drp_qa's twilight judge: judges a visit's IIC sequence once it is a finished twilight with every visit's
#: tables stored, and writes the verdicts to qadb (PIPE2D-1973, INSTRM-3089).
TWILIGHT_JUDGE = "$DRP_QA_DIR/bin.src/twilightQa.py"

#: Seconds a twilight judgement may take before it is killed: a sequence is a handful of visits' tables.
TWILIGHT_TIMEOUT = 300

#: twilightQa.py's exit statuses (its module docstring): what happened to the visit's sequence.
TWILIGHT_JUDGED = 0
TWILIGHT_PENDING = 3
TWILIGHT_NOT_TWILIGHT = 4
TWILIGHT_NOT_WRITTEN = 5
TWILIGHT_JUDGED_BAD = 6


@dataclass(frozen=True)
class TwilightCheck:
    """A queue item asking for a visit's twilight judgement alone, without its QA: `qa twilight <visit>`."""

    visit: int


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

        # INSTRM-3089: after each visit's load, drp_qa's twilightQa.py judges the visit's IIC sequence if it
        # is a twilight that has finished with every visit's tables stored, and writes the verdicts to
        # db.qadb while loading is on (to its log alone otherwise). It finds the sequence in the opdb, so
        # without a db.opdb nothing is judged. The path is checked like the pipeline's, for the same reason,
        # but only when it will be used.
        self.twilight_path = os.path.expandvars(TWILIGHT_JUDGE)
        unexpanded = UNEXPANDED_VAR.search(self.twilight_path)
        if unexpanded and self.opdb_host is not None:
            raise RuntimeError(
                f"twilight judge path {self.twilight_path!r} still contains {unexpanded.group()!r} "
                f"after expansion — is DRP_QA_DIR set in the actor's environment?"
            )
        self.twilight_timeout = TWILIGHT_TIMEOUT

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
                f"as {self.qadb.get('user', 'pfs')}, with {self.loader_path}"
            )
        if self.qadb is None:
            return "qadb: not loading (the config has no db.qadb)"
        return "qadb: not loading (switched off)"

    def describe_twilight(self):
        """One line on whether, and where, each visit's twilight sequence is judged."""
        if self.opdb_host is None:
            return "twilight: not judging sequences (the config has no db.opdb)"
        where = (
            f"into the Butler and qadb at {self.qadb['host']}:{self.qadb.get('port', 5436)}"
            if self.qadb_load
            else "into the Butler only (qadb loading is off: `qa twilight <visit>` loads it later)"
        )
        return (
            f"twilight: judging each visit's sequence with {self.twilight_path}, "
            f"opdb {self.opdb_host}:{self.opdb.get('port', 5432)} as {self.opdb.get('user', 'public_user')}, "
            f"verdicts {where}"
        )

    def opdb_args(self):
        """Return the opdb options the loader and the judge take, from the config's `db.opdb`.

        Both scripts default a missing key the same way (5432, `public_user`, `opdb`); the actor
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
        self.logger.info(f"Output collection: {self.output_collection}")
        self.logger.info(self.describe_qadb_load())
        self.logger.info(self.describe_twilight())

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

            visit_id = item.visit if isinstance(item, TwilightCheck) else item
            try:
                self._current_visit = visit_id
                if isinstance(item, TwilightCheck):
                    self.logger.info(f"Judging the twilight sequence of visit {visit_id} alone (qa twilight)")
                    self.judge_twilight(visit_id)
                    continue
                self.logger.info(f"Processing visit: {visit_id}")
                self.run_pipetask(visit_id)
                # Even after a failed or killed pipetask: the images it did judge are in the
                # Butler, and the loader loads what it finds.
                self.load_qadb(visit_id)
                # Likewise: the judge says what a sequence is still waiting for.
                self.judge_twilight(visit_id)
            except Exception as e:
                self.logger.warning(f"Error processing {visit_id=}: {e}")
            finally:
                self._current_visit = None

    def pipetask_cmd(self, visit_id):
        """Build the pipetask command line for a single visit."""
        # fmt: off
        return [
            "pipetask",
            "--long-log",
            "--log-level", ".=INFO",
            "run",
            "-j", f"{self.num_procs}",
            "-b", self.datastore,
            "-i", self.input_collections,
            "-o", self.output_collection,
            # The output chain stores the inputs flattened as they were at its first run, and
            # drpActor prepends a new run to drpActor/reductions with each new reduction pipeline:
            # without --rebase every later visit fails pipetask's consistency check, and with the
            # check passing it would read the stale snapshot instead of the current inputs.
            "--rebase",
            "-p", self.pipeline_path,
            "-d", f"visit = {visit_id}"
        ]
        # fmt: on

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
        cmd = self.pipetask_cmd(visit_id)
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

    def load_cmd(self, visit_id):
        """Build the loader's command line for a single visit; None when loading is off.

        `python` is the one on PATH, as `pipetask` is: the stack's, which the loader needs.
        """
        if not self.qadb_load:
            return None
        # fmt: off
        cmd = [
            "python", self.loader_path,
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
        # The opdb fills qadb's pfs_visit (description, design, issued_at: the dashboard's visit times).
        if self.opdb_host:
            cmd += self.opdb_args()
        return cmd

    def load_qadb(self, visit_id):
        """Load a visit's QA tables and verdicts into qadb, if configured; log the outcome.

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

    def twilight_cmd(self, visit_id):
        """Build the twilight judge's command line for a visit; None when the config has no db.opdb.

        The judge decides for itself whether the visit's sequence is a twilight, and whether it is
        complete; it stores its verdict in the output collection, and loads it into the same qadb the
        loader does while loading is on.
        """
        if self.opdb_host is None:
            return None
        # fmt: off
        cmd = [
            "python", self.twilight_path,
            "-b", self.datastore,
            "-c", self.output_collection,
            "--sequence-of-visit", f"{visit_id}",
            *self.opdb_args(),
        ]
        # fmt: on
        if self.qadb_load:
            # fmt: off
            cmd += [
                "--qadb-host", f"{self.qadb['host']}",
                "--qadb-port", f"{self.qadb.get('port', 5436)}",
                "--qadb-user", f"{self.qadb.get('user', 'pfs')}",
                "--qadb-dbname", f"{self.qadb.get('dbname', 'qadb')}",
            ]
            # fmt: on
        return cmd

    def judge_twilight(self, visit_id):
        """Judge a visit's twilight sequence if it is complete, and log what happened.

        Nothing happens until the sequence's last visit has been through QA: the call after that one
        judges it. The judge's exit status says which it was (`TWILIGHT_JUDGED`, ...): a sequence with a
        WARN or FAIL exposure is logged at WARNING, so it stands out in the log; on a failed qadb write
        it has printed the verdicts in full, so they are in the log above the warning.
        """
        cmd = self.twilight_cmd(visit_id)
        if cmd is None:
            return

        def watchdog(process, visit_id, timed_out):
            return self._start_watchdog(process, visit_id, timed_out, self.twilight_timeout, "twilight judge")

        returncode, timed_out = self._run_streamed(cmd, visit_id, watchdog)
        if timed_out and returncode != 0:
            self.logger.warning(
                f"twilight judge timed out for {visit_id=} after {self.twilight_timeout}s, killed"
            )
            self.logger.warning(f"Timed out command: {' '.join(cmd)}")
        elif returncode == TWILIGHT_JUDGED:
            self.logger.info(f"twilight sequence judged for {visit_id=}: every exposure PASS or TOO_FAINT")
        elif returncode == TWILIGHT_JUDGED_BAD:
            self.logger.warning(
                f"twilight sequence judged for {visit_id=}: some exposure is WARN or FAIL;"
                " the judge's output above says which, and why"
            )
        elif returncode == TWILIGHT_PENDING:
            self.logger.info(
                f"twilight sequence of {visit_id=} not complete yet; the judge's output says why"
            )
        elif returncode == TWILIGHT_NOT_TWILIGHT:
            self.logger.debug(f"{visit_id=} is not in a twilight sequence")
        elif returncode == TWILIGHT_NOT_WRITTEN:
            self.logger.warning(
                f"twilight sequence of {visit_id=} judged but not stored in the Butler or loaded into qadb;"
                f" the judge's output above says which failed and holds the verdicts, and"
                f" `qa twilight {visit_id}` judges it again"
            )
        else:
            self.logger.warning(f"twilight judge failed for {visit_id=} (returncode {returncode})")
            self.logger.warning(f"Failed command: {' '.join(cmd)}")

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

    def enqueue_twilight(self, visit_id):
        """Enqueue a visit's twilight judgement alone (`qa twilight <visit>`), behind the visits queued."""
        self.processing_queue.put(TwilightCheck(int(visit_id)))

    def queue_size(self):
        """Return the current number of visits waiting in the queue."""
        return self.processing_queue.qsize()
