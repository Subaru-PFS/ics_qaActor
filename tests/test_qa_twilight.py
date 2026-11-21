"""Tests for twilight sequences, which drp_qa's loader judges itself.

`drpQa.py load`, given the opdb, measures and judges a twilight sequence once a load finds it complete,
so the load of its last visit judges it: the actor runs one subprocess per visit after the chain, not a
load and a separate judge. `qa twilight <visit>` is a load of that visit alone. Subprocesses are replaced
by a fake Popen.
"""

import logging

import pytest

import qaActor.Controllers.qa as qaModule
from qaActor.Controllers.qa import LoadVisit, qa

from .conftest import FakeActor
from .test_qa_qadb import loading, popens, sharedConfig  # noqa: F401 — fixtures


def step(cmd):
    """Name a subprocess call: pipetask, the chain module, or the drp_qa script's file name and subcommand."""
    if cmd[0] != "python":
        return cmd[0]
    if cmd[1] == "-m":
        return cmd[2]
    return " ".join([cmd[1].rsplit("/", 1)[-1], cmd[2]])


@pytest.fixture
def judging(loading):  # noqa: F811
    """A controller with an opdb and qadb loading on: the loads judge twilight sequences."""
    return loading


class TestConfig:
    def test_the_separate_judge_is_gone(self):
        for name in (
            "TWILIGHT_JUDGE",
            "TWILIGHT_TIMEOUT",
            "TWILIGHT_JUDGED",
            "TWILIGHT_PENDING",
            "TWILIGHT_NOT_TWILIGHT",
            "TWILIGHT_NOT_WRITTEN",
            "TWILIGHT_JUDGED_BAD",
            "TwilightCheck",
        ):
            assert not hasattr(qaModule, name), name
        for name in ("twilight_cmd", "judge_twilight", "enqueue_twilight"):
            assert not hasattr(qa, name), name

    def test_an_unset_env_var_is_no_failure_without_a_loader(self, sharedConfig, logger, monkeypatch):  # noqa: F811
        # The judge's path check went with it: with an opdb but loading off, nothing names $DRP_QA_DIR.
        monkeypatch.delenv("DRP_QA_DIR", raising=False)
        sharedConfig["engine"]["pipeline"] = "/opt/pipelines/drpQA.yaml"
        sharedConfig["engine"]["qadb"] = {"load": False}
        ctrl = qa(FakeActor(sharedConfig, logger), "qa")
        assert ctrl.opdb_host == "db-ics" and ctrl.load_cmd(42) is None

    def test_describes_who_judges(self, judging):
        assert judging.describe_twilight() == (
            "twilight: the load of a sequence's last visit judges it, reading the sequence from"
            " opdb db-ics:5432 as pfs"
        )

    def test_without_a_db_opdb_nothing_is_judged(self, controller):
        assert controller.describe_twilight() == "twilight: not judging sequences (the config has no db.opdb)"

    def test_with_loading_off_nothing_is_judged(self, judging):
        judging.set_qadb_load(False)
        assert judging.describe_twilight().startswith("twilight: not judging sequences (qadb loading is off")
        # Negative control: switched back on, the loads judge again.
        judging.set_qadb_load(True)
        assert "judges it" in judging.describe_twilight()
        assert "not judging" not in judging.describe_twilight()

    def test_start_says_who_judges(self, judging, caplog):
        judging.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            judging.start()
        judging.stop()
        judging.join(timeout=5)
        assert "twilight: the load of a sequence's last visit judges it" in caplog.text
        loader = "/opt/drp_qa/bin.src/drpQa.py load"
        assert f"qadb: loading each visit to qadb-host:5436 as pfs, with {loader}" in caplog.text

    def test_the_load_gives_the_loader_the_opdb(self, judging):
        # Without --opdb-host the loader judges no twilight.
        cmd = judging.load_cmd(42)
        assert cmd[cmd.index("--opdb-host") + 1] == "db-ics"


class TestRunLoop:
    def test_each_visit_runs_one_load_and_no_judge(self, judging, popens):  # noqa: F811
        calls = popens(0, 0, 0, 0, 0, 0)
        judging.enqueue_visit(1)
        judging.enqueue_visit(2)
        judging.stop()
        judging.run()
        assert [step(cmd) for cmd in calls] == [
            "pipetask", "qaActor.chain", "drpQa.py load",
            "pipetask", "qaActor.chain", "drpQa.py load",
        ]  # fmt: skip
        # Negative control: the old second call per visit, and the removed scripts, never run.
        names = [cmd[1].rsplit("/", 1)[-1] for cmd in calls if cmd[0] == "python"]
        assert "twilightQa.py" not in names and "loadQadb.py" not in names
        assert not [cmd for cmd in calls if "--sequence-of-visit" in cmd]

    def test_one_drp_qa_subprocess_per_visit(self, judging, popens):  # noqa: F811
        calls = popens(0, 0, 0)
        judging.enqueue_visit(1)
        judging.stop()
        judging.run()
        loaderCalls = [cmd for cmd in calls if cmd[0] == "python" and cmd[1].startswith("/opt/drp_qa/")]
        assert loaderCalls == [judging.load_cmd(1)]
        # Negative control: the two calls per visit of before would be two here.
        assert len(loaderCalls) != 2

    def test_a_qa_twilight_runs_the_load_alone(self, judging, popens):  # noqa: F811
        calls = popens(0)
        judging.enqueue_load(42)
        judging.stop()
        judging.run()
        assert calls == [judging.load_cmd(42)]
        assert judging.current_visit is None

    def test_a_qa_twilight_queues_behind_visits(self, judging, popens):  # noqa: F811
        calls = popens(0, 0, 0, 0)
        judging.enqueue_visit(1)
        judging.enqueue_load(2)
        judging.stop()
        judging.run()
        assert [step(cmd) for cmd in calls] == ["pipetask", "qaActor.chain", "drpQa.py load", "drpQa.py load"]
        assert [cmd[cmd.index("--visits") + 1] for cmd in calls[2:]] == ["1", "2"]

    def test_a_qa_twilight_after_loading_was_switched_off_warns(self, judging, popens, caplog):  # noqa: F811
        calls = popens(0)
        judging.enqueue_load(42)
        judging.set_qadb_load(False)
        judging.stop()
        with caplog.at_level(logging.INFO):
            judging.run()
        assert calls == []
        assert "visit_id=42 not loaded: qadb: not loading (switched off)" in caplog.text

    def test_a_failed_load_does_not_stop_the_loop(self, judging, popens, caplog):  # noqa: F811
        calls = popens(1, 0)
        judging.enqueue_load(1)
        judging.enqueue_load(2)
        judging.stop()
        with caplog.at_level(logging.INFO):
            judging.run()
        assert len(calls) == 2
        assert "qadb load failed for visit_id=1" in caplog.text
        assert "qadb loaded for visit_id=2" in caplog.text

    def test_the_item_holds_a_plain_int(self, judging):
        judging.enqueue_load("42")
        item = judging.processing_queue.get_nowait()
        assert item == LoadVisit(42) and type(item.visit) is int

    def test_without_a_db_opdb_the_load_has_no_opdb(self, sharedConfig, logger, drpQaDir, popens):  # noqa: F811
        del sharedConfig["db"]["opdb"]
        ctrl = qa(FakeActor(sharedConfig, logger), "qa")
        calls = popens(0, 0, 0)
        ctrl.enqueue_visit(1)
        ctrl.stop()
        ctrl.run()
        assert [step(cmd) for cmd in calls] == ["pipetask", "qaActor.chain", "drpQa.py load"]
        assert "--opdb-host" not in calls[2]
