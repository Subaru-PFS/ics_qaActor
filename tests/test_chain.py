"""`qaActor.chain`: the output chain is the actor's RUNs, newest first, then the inputs as chained members."""

import sys
import types

import pytest

from qaActor import chain

OWN = ["qaActor/reductions/20260902T102447Z", "qaActor/reductions/20260902T102312Z"]
INPUTS = ["drpActor/reductions", "PFS/defaults"]
#: What pipetask's -o left behind (INSTRM-3096): the inputs flattened as they were, no chained member.
FROZEN = [*OWN, "drpActor/reductions/20260902T034923Z", "drpActor/reductions/20260901T201840Z", "PFS/raw/sps"]


class TestDesiredChain:
    def test_a_frozen_snapshot_is_repaired(self):
        assert chain.desired_chain(FROZEN, "qaActor/reductions", INPUTS) == [*OWN, *INPUTS]

    def test_a_run_is_prepended_once(self):
        run = "qaActor/reductions/20261106T200000Z"
        assert chain.desired_chain(OWN, "qaActor/reductions", INPUTS, run) == [run, *OWN, *INPUTS]
        # Prepending the run again moves nothing and duplicates nothing.
        assert chain.desired_chain([run, *OWN, *INPUTS], "qaActor/reductions", INPUTS, run) == [
            run,
            *OWN,
            *INPUTS,
        ]

    def test_own_runs_keep_their_order(self):
        assert chain.desired_chain(list(reversed(OWN)), "qaActor/reductions", INPUTS)[:2] == list(
            reversed(OWN)
        )

    def test_an_empty_chain_is_the_inputs(self):
        assert chain.desired_chain([], "qaActor/reductions", INPUTS) == INPUTS

    def test_the_flattened_inputs_are_not_kept(self):
        # Negative control: a flattened drpActor RUN is not an own RUN, however similar its name.
        desired = chain.desired_chain(FROZEN, "qaActor/reductions", INPUTS)
        assert "drpActor/reductions/20260902T034923Z" not in desired
        assert "PFS/raw/sps" not in desired
        # And an own RUN is kept: the prefix check is on the collection name with its slash.
        assert all(own in desired for own in OWN)
        assert "qaActor/reductions-other/20260902T000000Z" not in chain.desired_chain(
            ["qaActor/reductions-other/20260902T000000Z"], "qaActor/reductions", INPUTS
        )


class MissingCollectionError(LookupError):
    pass


class StubCollections:
    def __init__(self, chains, runs):
        self.chains = dict(chains)
        self.runs = set(runs)
        self.registered = []
        self.redefined = []

    def get_info(self, name):
        if name in self.chains:
            return types.SimpleNamespace(name=name, children=tuple(self.chains[name]))
        if name in self.runs:
            return types.SimpleNamespace(name=name, children=())
        raise MissingCollectionError(name)

    def register(self, name, kind):
        self.registered.append((name, kind))
        self.chains[name] = []

    def redefine_chain(self, name, children):
        self.redefined.append((name, list(children)))
        self.chains[name] = list(children)


@pytest.fixture
def lsst(monkeypatch):
    """A stand-in `lsst.daf.butler` with the two names `define_chain` imports."""
    module = types.ModuleType("lsst.daf.butler")
    module.CollectionType = types.SimpleNamespace(CHAINED="CHAINED")
    module.MissingCollectionError = MissingCollectionError
    monkeypatch.setitem(sys.modules, "lsst", types.ModuleType("lsst"))
    monkeypatch.setitem(sys.modules, "lsst.daf", types.ModuleType("lsst.daf"))
    monkeypatch.setitem(sys.modules, "lsst.daf.butler", module)
    return module


def butlerWith(chains, runs=()):
    return types.SimpleNamespace(collections=StubCollections(chains, runs))


class TestDefineChain:
    def test_repairs_a_frozen_chain(self, lsst):
        butler = butlerWith({"qaActor/reductions": FROZEN})
        said = []
        assert chain.define_chain(butler, "qaActor/reductions", INPUTS, log=said.append) is True
        assert butler.collections.redefined == [("qaActor/reductions", [*OWN, *INPUTS])]
        assert said == [
            "qaActor/reductions: repaired; 2 RUNs then drpActor/reductions, PFS/defaults;"
            " 3 flattened members dropped"
        ]

    def test_leaves_a_sound_chain_alone(self, lsst):
        butler = butlerWith({"qaActor/reductions": [*OWN, *INPUTS]})
        said = []
        assert chain.define_chain(butler, "qaActor/reductions", INPUTS, log=said.append) is False
        assert butler.collections.redefined == []
        assert said == ["qaActor/reductions: unchanged, 2 RUNs then drpActor/reductions, PFS/defaults"]

    def test_prepends_an_existing_run(self, lsst):
        run = "qaActor/reductions/20261106T200000Z"
        butler = butlerWith({"qaActor/reductions": [*OWN, *INPUTS]}, runs=[run])
        said = []
        assert chain.define_chain(butler, "qaActor/reductions", INPUTS, run, log=said.append) is True
        assert butler.collections.chains["qaActor/reductions"] == [run, *OWN, *INPUTS]
        assert said[0].startswith(f"qaActor/reductions: prepended {run}; 3 RUNs then")

    def test_a_run_pipetask_never_wrote_is_reported_not_chained(self, lsst):
        run = "qaActor/reductions/20261106T200000Z"
        butler = butlerWith({"qaActor/reductions": [*OWN, *INPUTS]})  # no such RUN
        said = []
        assert chain.define_chain(butler, "qaActor/reductions", INPUTS, run, log=said.append) is False
        assert said[0] == f"{run}: no such RUN (pipetask wrote nothing), not chained"
        assert run not in butler.collections.chains["qaActor/reductions"]

    def test_registers_a_missing_chain(self, lsst):
        butler = butlerWith({})
        said = []
        assert chain.define_chain(butler, "qaActor/reductions", INPUTS, log=said.append) is True
        assert butler.collections.registered == [("qaActor/reductions", "CHAINED")]
        assert butler.collections.chains["qaActor/reductions"] == INPUTS
        assert said[0] == "qaActor/reductions: registered as CHAINED"


class TestMain:
    def test_parses_the_controllers_command_line(self, lsst, monkeypatch):
        made = []
        butler = butlerWith({"qaActor/reductions": FROZEN}, runs=["qaActor/reductions/20261106T200000Z"])
        lsst.Butler = lambda repo, writeable: made.append((repo, writeable)) or butler
        argv = ["-b", "/work/datastore", "-c", "qaActor/reductions", "-i", "drpActor/reductions,PFS/defaults",
                "--run", "qaActor/reductions/20261106T200000Z"]  # fmt: skip
        assert chain.main(argv) == 0
        assert made == [("/work/datastore", True)]
        assert butler.collections.chains["qaActor/reductions"] == [
            "qaActor/reductions/20261106T200000Z", *OWN, *INPUTS,
        ]  # fmt: skip

    def test_the_run_is_optional(self, lsst):
        butler = butlerWith({"qaActor/reductions": [*OWN, *INPUTS]})
        lsst.Butler = lambda repo, writeable: butler
        assert (
            chain.main(["-b", "/b", "-c", "qaActor/reductions", "-i", "drpActor/reductions,PFS/defaults"])
            == 0
        )
        assert butler.collections.redefined == []
