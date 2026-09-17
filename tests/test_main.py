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
from RO import AddCallback

from qaActor import main as main_module
from qaActor.main import QaActor, main
from qaActor.models.drp import Drp

from .conftest import FakeCmd, FakeModel


class FakeKeyVar(AddCallback.BaseMixin):
    """A keyvar that records the callbacks registered against it.

    Built on the real `RO.AddCallback` mixin that opscore keyvars inherit, so
    the dedupe `connectionMade` leans on — `if callFunc not in self._callbacks`,
    which compares bound methods by identity — is the genuine implementation
    rather than a restatement of it that could drift.
    """

    def __init__(self):
        AddCallback.BaseMixin.__init__(self)
        self._callNow = {}

    def addCallback(self, callback, callNow=True):
        self._callNow.setdefault(callback, callNow)
        AddCallback.BaseMixin.addCallback(self, callback, callNow=callNow)

    @property
    def callbacks(self):
        """The registered callbacks, after the mixin's own deduping."""
        return [{"callback": f, "callNow": self._callNow[f]} for f in self._callbacks]


@pytest.fixture
def qaActor(monkeypatch, controller, logger):
    """Build a QaActor with ICC.__init__ bypassed and the ICC surface faked out."""
    monkeypatch.setattr(ICC, "__init__", lambda self, name, **kwargs: None)

    actor = QaActor("qa", productName="qaActor")
    actor.logger = logger
    actor.productName = "qaActor"
    actor.controllers = {}
    actor.models = {}
    actor.bcast = FakeCmd()
    actor.attached = []
    actor.addedModels = []

    actor.keyVar = FakeKeyVar()

    def fakeAttachAllControllers(path=None):
        actor.attached.append(path)
        # The real attachAllControllers instantiates and registers the controller.
        actor.controllers["qa"] = controller

    def fakeAddModels(names):
        actor.addedModels.append(names)
        for name in names:
            actor.models[name] = FakeModel({"reduceExposureStatus": actor.keyVar})

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
            {"callback": qaActor.drp.check_reduced_exposure_status, "callNow": False}
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


class TestReconnect:
    """`connectionMade` runs again on every hub reconnect, so it must be repeatable.

    Twisted drives it through a ReconnectingClientFactory; `attachController`
    builds a brand new controller, with a brand new queue, each time round.
    """

    def test_registers_the_callback_only_once_across_reconnects(self, qaActor):
        qaActor.connectionMade()
        qaActor.connectionMade()
        qaActor.connectionMade()

        # addCallback dedupes by identity only, so a fresh Drp per reconnect
        # would stack up one live callback each time.
        assert len(qaActor.keyVar.callbacks) == 1

    def test_keeps_the_same_drp_model_across_reconnects(self, qaActor):
        qaActor.connectionMade()
        first = qaActor.drp

        qaActor.connectionMade()

        assert qaActor.drp is first

    def test_visits_follow_the_controller_attached_by_the_latest_reconnect(
        self, qaActor, actorConfig, logger
    ):
        import opscore.protocols.types as types

        from qaActor.Controllers.qa import qa

        from .conftest import FakeActor, FakeKey

        qaActor.connectionMade()
        stale = qaActor.controllers["qa"]

        # Reconnect, and let it swap in a different controller the way the real
        # attachController does.
        replacement = qa(FakeActor(actorConfig, logger), "qa")
        qaActor.attachAllControllers = lambda path=None: qaActor.controllers.__setitem__("qa", replacement)
        qaActor.connectionMade()

        callback = qaActor.keyVar.callbacks[0]["callback"]
        callback(FakeKey(valueList=[types.Int()("12345")]))

        assert replacement.queue_size() == 1, "the live controller must receive the visit"
        assert stale.queue_size() == 0, "the orphaned controller must not be fed"


class TestControllerFailedToAttach:
    """`attachAllControllers` only logs construction failures, so check for it.

    Carrying on regardless leaves an actor that answers `ping` and quietly
    processes nothing at all.
    """

    def test_raises_when_the_controller_is_missing(self, qaActor):
        qaActor.attachAllControllers = lambda path=None: None

        with pytest.raises(RuntimeError, match="QA controller failed to attach"):
            qaActor.connectionMade()

    def test_warns_the_commanders_when_the_controller_is_missing(self, qaActor):
        qaActor.attachAllControllers = lambda path=None: None

        with pytest.raises(RuntimeError):
            qaActor.connectionMade()

        assert qaActor.bcast.warns == ['text="QA controller failed to attach; no visits will be processed"']

    def test_does_not_subscribe_to_anything_when_the_controller_is_missing(self, qaActor):
        qaActor.attachAllControllers = lambda path=None: None

        with pytest.raises(RuntimeError):
            qaActor.connectionMade()

        assert qaActor.addedModels == []
        assert qaActor.drp is None


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

        monkeypatch.setattr(main_module, "QaActor", FakeQaActor)
        main()

        assert built == {"name": "qa", "kwargs": {"productName": "qaActor"}, "ran": True}

    def test_rejects_unknown_arguments(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["qa_main", "--bogus"])
        with pytest.raises(SystemExit):
            main()
