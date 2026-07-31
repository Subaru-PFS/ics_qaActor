#!/usr/bin/env python

import argparse
from typing import override

from actorcore.ICC import ICC

from qaActor.models.drp import Drp


class QaActor(ICC):
    def __init__(self, name, **kwargs):
        self.allControllers = ["qa"]
        self.drp = None
        super().__init__(name, **kwargs)

    @override
    def connectionMade(self):
        self.logger.info("Connection made — starting QA controller")

        # Attaching the controller starts its processing loop.
        self.attachAllControllers()

        # Attach a model of an external actor so we can listen to its properties.
        _models = ("drp",)
        self.drp = Drp(
            actor=self, processing_queue=self.controllers["qa"].processing_queue, logger=self.logger
        )
        self.addModels(_models)

        # Add a listener on the Drp model for when reduceExposure task is complete.
        self.models["drp"].keyVarDict["reduceExposureStatus"].addCallback(
            self.drp.check_reduced_exposure_status, callNow=False
        )

    @override
    def connectionLost(self, reason):
        self.logger.info(f"Connection lost: {reason}")


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()

    actor = QaActor("qa", productName="qaActor")
    actor.run()


if __name__ == "__main__":
    main()
