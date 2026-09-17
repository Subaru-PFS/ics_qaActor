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
        """Wire the QA controller and the drp keyvar listener together.

        Twisted calls this through a reconnecting client factory, so it runs
        again on every hub reconnect, not just at startup. Everything here is
        therefore written to be safe to repeat.
        """
        self.logger.info("Connection made — starting QA controller")

        # Attaching the controller starts its processing loop.
        self.attachAllControllers()

        # attachAllControllers swallows construction failures and only logs them,
        # so check rather than trusting it: without a controller there is nothing
        # to feed, and silently carrying on leaves an actor that answers `ping`
        # while quietly processing nothing.
        if "qa" not in self.controllers:
            self.bcast.warn('text="QA controller failed to attach; no visits will be processed"')
            raise RuntimeError("QA controller failed to attach")

        # Attach a model of an external actor so we can listen to its properties.
        # `drp2` is a numbered drpActor instance; opscore strips the trailing
        # digits, so the keys still come from actorkeys/drp.py.
        _models = ("drp2",)
        self.addModels(_models)

        # Reuse the model across reconnects. `addCallback` skips a callback it
        # already holds, but only by identity — a fresh Drp would give a fresh
        # bound method and stack up one live callback per reconnect. The Drp
        # resolves the controller queue lazily, so the surviving instance always
        # feeds whichever controller is currently attached.
        if self.drp is None:
            self.drp = Drp(actor=self, logger=self.logger)

        # Add a listener on the Drp model for when reduceExposure task is complete.
        self.models["drp2"].keyVarDict["reduceExposureStatus"].addCallback(
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
