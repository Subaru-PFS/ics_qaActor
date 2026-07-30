import logging
import os
import queue
import threading

from qaActor.utils import run_qa_loop


class qa(threading.Thread):
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

    def stop(self, cmd=None):
        """Ask the QA processing loop to exit.

        The thread is a daemon, so it also goes away with the actor process; the
        sentinel just lets an idle loop unblock and exit cleanly.
        """
        self.logger.info("Stopping QA processing loop")
        self.processing_queue.put(None)

        if cmd:
            cmd.inform('text="QA processing loop stopped"')

    def run(self):
        run_qa_loop(
            visit_queue=self.processing_queue,
            input_collections=self.input_collections,
            output_collection=self.output_collection,
            pipeline_path=self.pipeline_path,
            datastore=self.datastore,
        )

    def enqueue_visit(self, visit_id):
        """Enqueue a visit for QA processing (called by the Drp model)."""
        self.processing_queue.put(visit_id)

    def queue_size(self):
        """Return the current number of visits waiting in the queue."""
        return self.processing_queue.qsize()
