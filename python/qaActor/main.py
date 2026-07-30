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
        self.logger.info("Connection made — starting QA supervisor")

        self.attachAllControllers()

        _models = ("drp",)
        self.drp = Drp(
            actor=self, processing_queue=self.controllers["qa"].processing_queue, logger=self.logger
        )
        self.addModels(_models)

        # Add a listener for when reduceExposure task is complete.
        self.models["drp"].keyVarDict["reduceExposureStatus"].addCallback(
            self.drp.receiveStatusKeys, callNow=False
        )
        self.controllers["qa"].start()

    @override
    def connectionLost(self, reason):
        self.logger.info("Connection lost — stopping QA supervisor")
        self.controllers["qa"].stop()


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()

    actor = QaActor("qa", productName="qaActor")
    actor.run()


if __name__ == "__main__":
    main()
