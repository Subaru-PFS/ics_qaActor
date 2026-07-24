#!/usr/bin/env python

import argparse
import multiprocessing

from actorcore.Actor import Actor

from qaActor.drp import Drp
from qaActor.utils import run_qa_for_visit


class QaActor(Actor):
    def __init__(self, name, **kwargs):
        super().__init__(name, **kwargs)

        self._is_connected = False
        self.drp = None

        # TODO update these and use instdata.
        input_collections = "PFS/calib/pipe2d-1861/run29/calib.20260708b,drpActor/reductions,PFS/defaults"
        output_collection = "qaActor/reductions"

        # Set up process queue with no max.
        self.processing_queue: multiprocessing.JoinableQueue = multiprocessing.JoinableQueue()
        self.worker = multiprocessing.Process(
            target=run_qa_for_visit,
            args=(self.processing_queue, input_collections, output_collection),
            daemon=True,
        )

    def connectionMade(self):
        if not self._is_connected:
            self.logger.info("Setting up qaActor to listen for reduceExposureStatus key from drp...")
            self._is_connected = True

            # Set up model to listen to drpActor and add visit to processing queue.
            self.drp = Drp(actor=self, processing_queue=self.processing_queue, logger=self.logger)
            _models = ("drp",)
            self.addModels(_models)
            self.models["drp"].keyVarDict["reduceExposureStatus"].addCallback(
                self.drp.receiveStatusKeys, callNow=False
            )

            # Start processing queue.
            self.logger.info("Starting processing worker.")
            self.worker.start()

    def connectionLost(self, reason):
        self.logger.info("Shutting down QA processing queue and worker")
        self.processing_queue.put(None)

        # Wait for the thread to finish processing remaining items
        self.logger.info("Waiting for QA worker thread to join")
        self.worker.join()
        self.logger.info("qaActor shutting down")
        self._is_connected = False


def main():
    parser = argparse.ArgumentParser()
    args = parser.parse_args()

    actor = QaActor("qa", productName="qaActor")
    actor.run()


if __name__ == "__main__":
    main()
