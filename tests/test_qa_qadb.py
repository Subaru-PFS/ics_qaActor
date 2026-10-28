"""Tests for loading each visit into qadb after its pipetask (INSTRM-3040).

The loader is drp_qa's `bin.src/loadQadb.py`, run as a subprocess the way
`pipetask` is; it is replaced by a fake Popen, or by a short Python program
where the point is the watchdog.
"""

import logging
import subprocess
import sys

import pytest

from qaActor.Controllers.qa import DEFAULT_LOAD_TIMEOUT, qa

from .conftest import FakeActor
from .test_qa_controller import FakePopen

#: The shared config's databases (actors.yaml), as merged into the actor's.
DB = {
    "opdb": {"host": "db-ics", "dbname": "opdb", "port": 5432, "user": "pfs"},
    "qadb": {"host": "qadb-host", "dbname": "qadb", "port": 5436, "user": "pfs"},
}


@pytest.fixture
def sharedConfig(actorConfig):
    """The qa config with actors.yaml's `db` section merged in, and loading switched on."""
    actorConfig["db"] = {name: dict(params) for name, params in DB.items()}
    actorConfig["engine"]["qadb"] = {"load": True}
    return actorConfig


@pytest.fixture
def loading(sharedConfig, logger, drpQaDir):
    """A controller configured to load into qadb."""
    return qa(FakeActor(sharedConfig, logger), "qa")


@pytest.fixture
def popens(monkeypatch):
    """Record every Popen call; each returns a process exiting with the next code given."""
    calls = []

    def factory(*exitCodes):
        codes = list(exitCodes)

        def fake(cmd, **kwargs):
            proc = FakePopen((), codes.pop(0) if codes else 0)
            calls.append(cmd)
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake)
        return calls

    return factory


class TestConfig:
    def test_without_a_qadb_section_nothing_is_loaded(self, controller, popens):
        calls = popens()
        assert controller.qadb is None and controller.load_cmd(42) is None
        controller.load_qadb(42)
        assert calls == []

    def test_the_shared_db_section_alone_does_not_switch_loading_on(self, sharedConfig, logger, drpQaDir):
        # Every actor gets db.qadb from actors.yaml; only qa.yaml's engine.qadb.load loads.
        del sharedConfig["engine"]["qadb"]
        assert qa(FakeActor(sharedConfig, logger), "qa").qadb is None

    def test_loading_without_a_db_qadb_fails_at_construction(self, sharedConfig, logger, drpQaDir):
        del sharedConfig["db"]["qadb"]
        with pytest.raises(RuntimeError, match=r"db\.qadb"):
            qa(FakeActor(sharedConfig, logger), "qa")

    def test_connects_where_the_shared_config_says(self, loading):
        assert loading.qadb == DB["qadb"] and loading.opdb_host == "db-ics"

    def test_the_loader_path_is_expanded_against_the_environment(self, loading):
        assert loading.loader_path == "/opt/drp_qa/bin.src/loadQadb.py"

    def test_an_unset_loader_env_var_fails_at_construction(self, sharedConfig, logger, monkeypatch):
        sharedConfig["engine"]["pipeline"] = "/literal/drpQA.yaml"
        monkeypatch.delenv("DRP_QA_DIR", raising=False)
        with pytest.raises(RuntimeError, match="DRP_QA_DIR"):
            qa(FakeActor(sharedConfig, logger), "qa")

    def test_the_load_timeout_defaults_and_is_configurable(self, loading, sharedConfig, logger, drpQaDir):
        assert loading.load_timeout == DEFAULT_LOAD_TIMEOUT
        sharedConfig["engine"]["qadb"]["timeout"] = 60
        assert qa(FakeActor(sharedConfig, logger), "qa").load_timeout == 60

    def test_start_says_where_it_loads(self, loading, caplog):
        loading.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            loading.start()
        assert "qadb: loading each visit to qadb-host:5436 as pfs" in caplog.text

    def test_start_says_when_it_does_not_load(self, controller, caplog):
        controller.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            controller.start()
        assert "qadb: not loading" in caplog.text


class TestLoadCmd:
    def test_builds_the_exact_command_line(self, loading):
        assert loading.load_cmd(42) == [
            "python", "/opt/drp_qa/bin.src/loadQadb.py",
            "-b", "/work/datastore",
            "-c", "qaActor/reductions",
            "--visits", "42",
            "--mode", "realtime",
            "--host", "qadb-host",
            "--port", "5436",
            "--user", "pfs",
            "--dbname", "qadb",
            "--opdb-host", "db-ics",
        ]  # fmt: skip

    def test_loads_the_collection_the_pipeline_writes(self, loading):
        cmd = loading.load_cmd(42)
        assert cmd[cmd.index("-c") + 1] == loading.output_collection

    def test_without_an_opdb_host_the_flag_is_left_out(self, sharedConfig, logger, drpQaDir):
        del sharedConfig["db"]["opdb"]
        sharedConfig["db"]["qadb"] = {"host": "qadb-host"}
        cmd = qa(FakeActor(sharedConfig, logger), "qa").load_cmd(42)
        assert "--opdb-host" not in cmd
        # The loader's own defaults, spelled out.
        assert cmd[cmd.index("--port") + 1] == "5436" and cmd[cmd.index("--user") + 1] == "pfs"

    def test_every_argument_is_a_string(self, loading):
        assert all(isinstance(arg, str) for arg in loading.load_cmd(42))


class TestLoadQadb:
    def test_logs_success(self, loading, popens, caplog):
        calls = popens(0)
        with caplog.at_level(logging.INFO):
            loading.load_qadb(42)
        assert calls == [loading.load_cmd(42)]
        assert "qadb loaded for visit_id=42" in caplog.text

    def test_warns_on_failure(self, loading, popens, caplog):
        popens(1)
        with caplog.at_level(logging.INFO):
            loading.load_qadb(42)
        assert "qadb load failed for visit_id=42 (returncode 1)" in caplog.text
        assert "qadb loaded" not in caplog.text

    def test_a_hung_load_is_killed_with_its_own_timeout(self, loading, monkeypatch, caplog):
        program = "import time; time.sleep(30)"
        monkeypatch.setattr(loading, "load_cmd", lambda visitId: [sys.executable, "-c", program])
        loading.load_timeout = 0.5
        loading.timeout = 600  # the pipetask's, which must not apply here
        with caplog.at_level(logging.INFO):
            loading.load_qadb(42)
        assert "qadb load for visit_id=42 exceeded 0.5s, killing it" in caplog.text
        assert "qadb load timed out for visit_id=42 after 0.5s, killed" in caplog.text


class TestRunLoop:
    def test_each_visit_is_loaded_after_its_pipetask(self, loading, popens):
        calls = popens(0, 0, 0, 0)
        loading.enqueue_visit(1)
        loading.enqueue_visit(2)
        loading.stop()
        loading.run()
        assert [cmd[0] for cmd in calls] == ["pipetask", "python", "pipetask", "python"]
        assert [cmd[cmd.index("--visits") + 1] for cmd in calls[1::2]] == ["1", "2"]

    def test_a_failed_pipetask_is_still_loaded(self, loading, popens):
        # pipetask exits non-zero when any quantum fails; the other images' verdicts are in the Butler.
        calls = popens(3, 0)
        loading.enqueue_visit(1)
        loading.stop()
        loading.run()
        assert [cmd[0] for cmd in calls] == ["pipetask", "python"]

    def test_a_failed_load_does_not_stop_the_loop(self, loading, popens, caplog):
        calls = popens(0, 1, 0, 0)
        loading.enqueue_visit(1)
        loading.enqueue_visit(2)
        loading.stop()
        with caplog.at_level(logging.INFO):
            loading.run()
        assert len(calls) == 4
        assert (
            "qadb load failed for visit_id=1" in caplog.text and "qadb loaded for visit_id=2" in caplog.text
        )
