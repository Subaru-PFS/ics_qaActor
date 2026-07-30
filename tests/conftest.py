"""Shared fixtures for the qaActor test suite.

These tests run without a set up EUPS environment. The only thing that actually
blocks imports is `actorcore.Actor`, which does `import eups` at module scope but
only *uses* it inside `Actor.__init__` — which no test ever calls. So a stub
module is enough to reach the qaActor code underneath.

Everything else (`actorcore.ICC`, `opscore.protocols`) imports and works offline,
so the tests use the real thing rather than mocks wherever they can.
"""

import logging
import queue
import sys
import types

import pytest

# ------------------------------------------------------------------------------
# EUPS stub — must be installed before anything imports actorcore.Actor.
# ------------------------------------------------------------------------------
if "eups" not in sys.modules:
    _eups = types.ModuleType("eups")

    class _StubEups:
        """Stands in for `eups.Eups`; loud if anything actually tries to use it."""

        def findProducts(self, *args, **kwargs):
            raise RuntimeError("no EUPS environment: eups.Eups is stubbed for tests")

    _eups.Eups = _StubEups
    sys.modules["eups"] = _eups


# ------------------------------------------------------------------------------
# Test doubles
# ------------------------------------------------------------------------------
class FakeCmd:
    """Records what the code under test reports back over MHS."""

    def __init__(self):
        self.informs = []
        self.warns = []
        self.finishes = []

    def inform(self, msg):
        self.informs.append(msg)

    def warn(self, msg):
        self.warns.append(msg)

    def finish(self, msg=""):
        self.finishes.append(msg)

    @property
    def finished(self):
        return bool(self.finishes)

    @property
    def messages(self):
        return self.informs + self.warns + self.finishes


class FakeActor:
    """The slice of the actor API that the controller, model and commands touch."""

    def __init__(self, actorConfig, logger):
        self.actorConfig = actorConfig
        self.logger = logger
        self.productName = "qaActor"
        self.controllers = {}
        self.models = {}
        self.bcast = FakeCmd()


class FakeModel:
    """An MHS model exposing a keyVarDict, as `QaCmd.show` expects."""

    def __init__(self, keyVarDict):
        self.keyVarDict = keyVarDict


class FakeKey:
    """A keyvar callback payload shaped like the one drpActor publishes."""

    def __init__(self, name="reduceExposureStatus", valueList=(), isCurrent=True, isGenuine=True):
        self.actor = "drp"
        self.name = name
        self.timestamp = 1700000000.0
        self.isCurrent = isCurrent
        self.isGenuine = isGenuine
        self.valueList = list(valueList)


# ------------------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------------------
@pytest.fixture
def logger(request):
    """A logger scoped to one test, restored afterwards.

    The controller mutates the actor's logger level in `__init__`, so each test
    gets its own to keep that from leaking.
    """
    log = logging.getLogger(f"qaActor.test.{request.node.name}")
    previousLevel = log.level
    yield log
    log.setLevel(previousLevel)


@pytest.fixture
def actorConfig():
    """A config with the shape documented in the README / qa.yaml."""
    return {
        "engine": {
            "butler": {
                "datastore": "/work/datastore",
                "input": ["PFS/calib/2024", "drpActor/reductions", "PFS/defaults"],
                "output": "qaActor/reductions",
            },
            "pipeline": "$DRP_QA_DIR/pipelines/drpQA.yaml",
            "num_procs": 4,
        }
    }


@pytest.fixture
def drpQaDir(monkeypatch):
    """Set the env var the pipeline path is expanded against."""
    monkeypatch.setenv("DRP_QA_DIR", "/opt/drp_qa")
    return "/opt/drp_qa"


@pytest.fixture
def actor(actorConfig, logger):
    return FakeActor(actorConfig, logger)


@pytest.fixture
def controller(actor, drpQaDir):
    """A `qa` controller, guaranteed not to leak a running thread.

    Several tests call the real `start`, so the teardown stops and joins the
    thread rather than relying on it being a daemon.
    """
    from qaActor.Controllers.qa import qa

    ctrl = qa(actor, "qa")
    yield ctrl

    if ctrl.is_alive():
        ctrl.stop()
        ctrl.join(timeout=5)
        assert not ctrl.is_alive(), "controller thread did not exit after stop()"


@pytest.fixture
def cmd():
    return FakeCmd()


@pytest.fixture
def processingQueue():
    return queue.Queue()
