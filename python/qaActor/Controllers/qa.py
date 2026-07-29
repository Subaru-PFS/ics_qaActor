import enum
import logging
import os
import queue
import threading

from qaActor.utils import run_qa_loop


class QaMode(enum.IntFlag):
    OFF = 0
    ON = 1
    STOP = 32


class QaThread(threading.Thread):
    def __init__(self, visit_queue, input_collections, output_collection, pipeline_path, datastore):
        super().__init__(daemon=True, name="QaThread")
        self.visit_queue = visit_queue
        self.input_collections = input_collections
        self.output_collection = output_collection
        self.pipeline_path = pipeline_path
        self.datastore = datastore

    def run(self):
        run_qa_loop(
            visit_queue=self.visit_queue,
            input_collections=self.input_collections,
            output_collection=self.output_collection,
            pipeline_path=self.pipeline_path,
            datastore=self.datastore,
        )


class QaSupervisor:
    Mode = QaMode

    def __init__(self, actor, name: str, logLevel: int = logging.DEBUG):
        self.actor = actor
        self.logger = actor.logger
        self.logger.setLevel(logLevel)

        self.logger.info(f"Setting up QA with {name=}")

        cfg = actor.actorConfig["engine"]
        self.datastore = cfg["butler"]["datastore"]
        self.input_collections = cfg["butler"]["input"]
        self.output_collection = cfg["butler"]["output"]
        self.pipeline_path = os.path.expandvars(cfg["pipeline"])

        self.mode = QaMode.OFF
        self._visit_queue = queue.Queue()
        self._thread = None

    def start(self, cmd=None):
        """Start the QA worker thread and register the drp model callback."""
        if self.mode == QaMode.ON:
            msg = "QA worker is already running"
            self.logger.warning(msg)
            if cmd:
                cmd.warn(f'text="{msg}"')
                cmd.finish()
            return

        self.logger.info("Starting QA worker thread")
        self._visit_queue = queue.Queue()
        self._thread = QaThread(
            visit_queue=self._visit_queue,
            input_collections=self.input_collections,
            output_collection=self.output_collection,
            pipeline_path=self.pipeline_path,
            datastore=self.datastore,
        )
        self._thread.start()
        self.mode = QaMode.ON

        self.logger.info("QA worker thread started")
        if cmd:
            cmd.inform('text="QA worker started"')
            cmd.finish()

    def stop(self, cmd=None):
        """Stop the QA worker thread."""
        if self.mode != QaMode.ON:
            msg = "QA worker is not running"
            self.logger.warning(msg)
            if cmd:
                cmd.warn(f'text="{msg}"')
                cmd.finish()
            return

        self.logger.info("Stopping QA worker thread")
        self.mode = QaMode.STOP
        self._visit_queue.put(None)
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.mode = QaMode.OFF

        self.logger.info("QA worker thread stopped")
        if cmd:
            cmd.inform('text="QA worker stopped"')
            cmd.finish()

    def restart(self, cmd=None):
        """Restart the QA worker thread."""
        self.logger.info("Restarting QA worker thread")
        self.stop()
        self.start(cmd=cmd)

    def enqueue_visit(self, visit_id):
        """Enqueue a visit for QA processing (called by the Drp model)."""
        self._visit_queue.put(visit_id)

    def queue_size(self):
        """Return the current number of visits waiting in the queue."""
        return self._visit_queue.qsize()


# Backward-compat alias
qa = QaSupervisor
