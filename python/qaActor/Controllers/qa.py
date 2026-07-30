import logging
import os
import queue
import subprocess
import threading


class qa(threading.Thread):  # noqa: N801 — name must match the module for ICC.attachController
    """QA processing loop.

    Runs for the lifetime of the actor: `start` and `stop` are the controller
    lifecycle hooks called by `ICC.attachController` / `ICC.detachController`,
    not user-facing commands.
    """

    def __init__(self, actor, name: str, logLevel: int = logging.DEBUG):
        super().__init__(daemon=True, name=name)
        self.actor = actor
        self.logger = actor.logger
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

        self.processing_queue = queue.Queue()

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
        sentinel just lets an idle loop unblock and exit cleanly.
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
                self.logger.info(f"Processing visit: {visit_id}")
                self.run_pipetask(visit_id)
            except Exception as e:
                self.logger.warning(f"Error processing {visit_id=}: {e}")

    def pipetask_cmd(self, visit_id):
        """Build the pipetask command line for a single visit."""
        # fmt: off
        return [
            "pipetask",
            "--long-log",
            "--log-level", ".=INFO",
            "--no-log-tty",
            "run",
            "-j", f"{self.num_procs}",
            "-b", self.datastore,
            "-i", self.input_collections,
            "-o", self.output_collection,
            "-p", self.pipeline_path,
            "-d", f"visit = {visit_id}",
            "--extend-run",
        ]
        # fmt: on

    def run_pipetask(self, visit_id):
        """Run the QA pipeline for a single visit, relaying pipetask output to the log."""
        cmd = self.pipetask_cmd(visit_id)
        self.logger.info(f"Running: {' '.join(cmd)}")

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        stdout_lines = []
        stderr_lines = []

        for line in process.stdout:
            line = line.rstrip()
            stdout_lines.append(line)
            self.logger.info(line)

        for line in process.stderr:
            line = line.rstrip()
            stderr_lines.append(line)
            self.logger.warning(f"STDERR: {line}")

        process.wait()

        if process.returncode != 0:
            self.logger.warning(f"QA pipetask failed for {visit_id=} (returncode {process.returncode})")
            self.logger.warning(f"Failed command: {' '.join(cmd)}")
            if stderr_lines:
                self.logger.warning(f"Error output ({len(stderr_lines)} lines):")
                for line in stderr_lines:
                    self.logger.warning(f"  {line}")
        else:
            self.logger.info(f"QA complete for {visit_id=}")

    def enqueue_visit(self, visit_id):
        """Enqueue a visit for QA processing (called by the Drp model)."""
        self.processing_queue.put(visit_id)

    def queue_size(self):
        """Return the current number of visits waiting in the queue."""
        return self.processing_queue.qsize()
