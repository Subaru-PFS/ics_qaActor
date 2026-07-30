"""Tests for the actor entry point (qaActor/main.py).

`ICC.__init__` needs a live tron connection and a set up EUPS product, so it is
replaced here. What is worth testing is `QaActor`'s own wiring: the order in
which it attaches controllers, builds the Drp model and registers the keyvar
callback. The stand-ins for `attachAllControllers` and `addModels` populate
`controllers` / `models` the way the real ones do, so getting that order wrong
fails these tests with a KeyError rather than passing quietly.
"""

import logging

import pytest
from actorcore.ICC import ICC

import qaActor.main as mainModule
from qaActor.main import QaActor, main
from qaActor.models.drp import Drp

from .conftest import FakeCmd, FakeModel


class FakeKeyVar:
    """A keyvar that records the callbacks registered against it."""

    def __init__(self):
        self.callbacks = []

    def addCallback(self, callback, callNow=True):
        self.callbacks.append({"callback": callback, "callNow": callNow})


@pytest.fixture
def qaActor(monkeypatch, controller, logger):
    """A QaActor with ICC.__init__ bypassed and the ICC surface faked out."""
    monkeypatch.setattr(ICC, "__init__", lambda self, name, **kwargs: None)

    actor = QaActor("qa", productName="qaActor")
    actor.logger = logger
    actor.productName = "qaActor"
    actor.controllers = {}
    actor.models = {}
    actor.bcast = FakeCmd()
    actor.attached = []
    actor.addedModels = []

    keyVar = FakeKeyVar()
    actor.keyVar = keyVar

    def fakeAttachAllControllers(path=None):
        actor.attached.append(path)
        # The real attachAllControllers instantiates and registers the controller.
        actor.controllers["qa"] = controller

    def fakeAddModels(names):
        actor.addedModels.append(names)
        for name in names:
            actor.models[name] = FakeModel({"reduceExposureStatus": keyVar})

    actor.attachAllControllers = fakeAttachAllControllers
    actor.addModels = fakeAddModels
    return actor


class TestConstruction:
    def test_declares_the_qa_controller(self, qaActor):
        assert qaActor.allControllers == ["qa"]

    def test_has_no_drp_model_until_the_connection_is_made(self, qaActor):
        assert qaActor.drp is None

    def test_is_an_ICC(self):
        assert issubclass(QaActor, ICC)

    def test_passes_the_name_and_kwargs_through_to_ICC(self, monkeypatch):
        recorded = {}

        def fakeInit(self, name, **kwargs):
            recorded["name"] = name
            recorded["kwargs"] = kwargs

        monkeypatch.setattr(ICC, "__init__", fakeInit)
        QaActor("qa", productName="qaActor")

        assert recorded == {"name": "qa", "kwargs": {"productName": "qaActor"}}


class TestConnectionMade:
    def test_attaches_the_controllers(self, qaActor):
        qaActor.connectionMade()
        assert qaActor.attached == [None]
        assert "qa" in qaActor.controllers

    def test_builds_the_drp_model_on_the_controller_queue(self, qaActor, controller):
        qaActor.connectionMade()

        assert isinstance(qaActor.drp, Drp)
        assert qaActor.drp.queue is controller.processing_queue
        assert qaActor.drp.actor is qaActor

    def test_subscribes_to_the_drp_model(self, qaActor):
        qaActor.connectionMade()
        assert qaActor.addedModels == [("drp",)]

    def test_registers_the_status_callback_without_firing_it(self, qaActor):
        qaActor.connectionMade()

        assert qaActor.keyVar.callbacks == [
            {"callback": qaActor.drp.receiveStatusKeys, "callNow": False}
        ]

    def test_a_key_delivered_to_the_callback_reaches_the_controller_queue(self, qaActor, controller):
        """End to end through the wiring: keyvar callback -> queue -> controller."""
        import opscore.protocols.types as types

        from .conftest import FakeKey

        qaActor.connectionMade()
        callback = qaActor.keyVar.callbacks[0]["callback"]

        callback(FakeKey(valueList=[types.Int()("12345")]))

        assert controller.queue_size() == 1
        assert controller.processing_queue.get_nowait() == 12345

    def test_logs_that_it_is_starting(self, qaActor, caplog):
        with caplog.at_level(logging.INFO):
            qaActor.connectionMade()

        assert "starting QA controller" in caplog.text


class TestConnectionLost:
    def test_logs_the_reason(self, qaActor, caplog):
        with caplog.at_level(logging.INFO):
            qaActor.connectionLost("connection reset by peer")

        assert "Connection lost: connection reset by peer" in caplog.text


class TestMain:
    def test_builds_the_actor_and_runs_it(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["qa_main"])
        built = {}

        class FakeQaActor:
            def __init__(self, name, **kwargs):
                built["name"] = name
                built["kwargs"] = kwargs
                built["ran"] = False

            def run(self):
                built["ran"] = True

        monkeypatch.setattr(mainModule, "QaActor", FakeQaActor)
        main()

        assert built == {"name": "qa", "kwargs": {"productName": "qaActor"}, "ran": True}

    def test_rejects_unknown_arguments(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["qa_main", "--bogus"])
        with pytest.raises(SystemExit):
            main()
