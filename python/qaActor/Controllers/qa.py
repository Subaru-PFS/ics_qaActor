import logging
import os
import queue
import subprocess
import threading

#: Seconds a single `pipetask` run may take before it is killed. Overridable as
#: `engine.timeout` in qa.yaml; set it to 0 or null there to disable the watchdog.
DEFAULT_TIMEOUT = 3600


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
        self.timeout = cfg.get("timeout", DEFAULT_TIMEOUT)

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
            watchdog = self._start_watchdog(process, visit_id, timed_out)
            try:
                for line in process.stdout:
                    self.logger.info(line.rstrip())
                process.wait()
            finally:
                if watchdog is not None:
                    watchdog.cancel()

        # `and returncode` guards the narrow race where the watchdog fires just
        # as the pipeline exits cleanly: a killed child reports a signal exit, so
        # a zero return code means it really did finish in time.
        if timed_out.is_set() and process.returncode != 0:
            self.logger.warning(f"QA pipetask timed out for {visit_id=} after {self.timeout}s, killed")
            self.logger.warning(f"Timed out command: {' '.join(cmd)}")
        elif process.returncode != 0:
            self.logger.warning(f"QA pipetask failed for {visit_id=} (returncode {process.returncode})")
            self.logger.warning(f"Failed command: {' '.join(cmd)}")
        else:
            self.logger.info(f"QA complete for {visit_id=}")

    def _start_watchdog(self, process, visit_id, timed_out):
        """Arm a timer that kills `process` once `self.timeout` has elapsed.

        Returns None when no timeout is configured.
        """
        if not self.timeout:
            return None

        def kill():
            timed_out.set()
            self.logger.warning(f"QA pipetask for {visit_id=} exceeded {self.timeout}s, killing it")
            process.kill()

        watchdog = threading.Timer(self.timeout, kill)
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
