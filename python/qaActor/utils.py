import os
import logging
import subprocess
from multiprocessing.queues import JoinableQueue

logger = logging.getLogger(__name__)


PIPELINE_PATH = os.path.expandvars("$DRP_QA_DIR/pipelines/drpQA.yaml#imageQualityQa")


def run_qa_for_visit(visit_queue: JoinableQueue, input_collections: list[str], output_collection):
    """The consumer loop running in a background process."""
    while True:
        # Blocks until new message received.
        logger.info("Checking QA processing queue for visits")
        visit_id = visit_queue.get()

        # Check for the sentinel value to shut down cleanly.
        if visit_id is None:
            logger.warning("Received a value of None in the queue, exiting")
            break

        try:
            logger.info(f"Processing visit: {visit_id}")
            # Run QA pipeline
            # fmt: off
            cmd = [
                "pipetask",
                "--long-log",
                "--log-level", ".=INFO",
                "--no-log-tty",
                "run",
                "-j", "24",
                "-b", "/work/datastore",
                "-i", input_collections,
                "-o", output_collection,
                "-p", PIPELINE_PATH,
                "-d", f"visit = {visit_id}",
                "--extend-run",
            ]
            # fmt: on
            run_pipetask(cmd, visit_id)
            logger.info(f"QA complete for {visit_id=}")
        except Exception as e:
            logger.warning(f"Error processing {visit_id=}: {e}")
        finally:
            visit_queue.task_done()


def run_pipetask(cmd, visit_id):
    logger.info(f"Starting pipetask process for {visit_id=}")
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in process.stdout:
        logger.info(line.rstrip())
    process.wait()

    if process.returncode != 0:
        logger.warning(f"QA pipetask failed for {visit_id=}")
    else:
        logger.info(f"QA pipetask complete for {visit_id=}")
