import logging
import os
import queue
import re
import subprocess
import threading

#: A `$VAR` or `${VAR}` that `os.path.expandvars` left behind, meaning it was unset.
UNEXPANDED_VAR = re.compile(r"\$\{?\w+")

#: Seconds a single `pipetask` run may take before it is killed. Overridable as
#: `engine.timeout` in qa.yaml; set it to 0 or null there to disable the watchdog.
DEFAULT_TIMEOUT = 600

#: Seconds a qadb load may take before it is killed. Overridable as `engine.qadb.timeout`.
DEFAULT_LOAD_TIMEOUT = 300

#: drp_qa's loader, which copies a visit's QA tables from the Butler into qadb (PIPE2D-1385).
DEFAULT_LOADER = "$DRP_QA_DIR/bin.src/loadQadb.py"


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

        # Optional (INSTRM-3040): with `engine.qadb.load`, each visit's verdicts are copied into the
        # qadb that the shared config (actors.yaml, merged into this one) names as `db.qadb`; without
        # it they stay in the Butler only. The opdb, `db.opdb`, fills qadb's visit rows.
        load = cfg.get("qadb") or {}
        self.qadb = None
        if load.get("load"):
            databases = actor.actorConfig.get("db") or {}
            if not databases.get("qadb"):
                raise RuntimeError("engine.qadb.load is set but the config has no db.qadb (actors.yaml)")
            self.qadb = databases["qadb"]
            self.opdb_host = (databases.get("opdb") or {}).get("host")
            self.loader_path = os.path.expandvars(load.get("loader", DEFAULT_LOADER))
            unexpanded = UNEXPANDED_VAR.search(self.loader_path)
            if unexpanded:
                raise RuntimeError(
                    f"qadb loader path {self.loader_path!r} still contains {unexpanded.group()!r} "
                    f"after expansion — is DRP_QA_DIR set in the actor's environment?"
                )
            self.load_timeout = load.get("timeout", DEFAULT_LOAD_TIMEOUT)

        self.processing_queue = queue.Queue()

        self._current_visit = None

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
        if self.qadb is None:
            self.logger.info("qadb: not loading (engine.qadb.load is not set)")
        else:
            self.logger.info(
                f"qadb: loading each visit to {self.qadb['host']}:{self.qadb.get('port', 5436)} "
                f"as {self.qadb.get('user', 'pfs')}, with {self.loader_path}"
            )

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
            visit_id = self.processing_queue.get()

            # `stop` enqueues None as the shutdown sentinel.
            if visit_id is None:
                self.logger.info("Received the stop sentinel, exiting QA processing loop")
                break

            try:
                self._current_visit = visit_id
                self.logger.info(f"Processing visit: {visit_id}")
                self.run_pipetask(visit_id)
                # Even after a failed or killed pipetask: the images it did judge are in the
                # Butler, and the loader loads what it finds.
                self.load_qadb(visit_id)
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
        """Build the loader's command line for a single visit; None when qadb is not configured.

        `python` is the one on PATH, as `pipetask` is: the stack's, which the loader needs.
        """
        if self.qadb is None:
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
            cmd += ["--opdb-host", f"{self.opdb_host}"]
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

    def queue_size(self):
        """Return the current number of visits waiting in the queue."""
        return self.processing_queue.qsize()
