"""Tests for loading each visit into qadb after its pipetask (INSTRM-3040).

The loader is drp_qa's `bin.src/drpQa.py load`, run as a subprocess
the way `pipetask` is; it is replaced by a fake Popen, or by a short Python program
where the point is the watchdog.
"""

import logging
import subprocess
import sys

import pytest

from qaActor.Controllers.qa import DEFAULT_LOAD_TIMEOUT, MarkFailed, qa

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

    def test_loading_defaults_on_when_the_config_has_a_db_qadb(self, sharedConfig, logger, drpQaDir):
        del sharedConfig["engine"]["qadb"]
        assert qa(FakeActor(sharedConfig, logger), "qa").load_cmd(42) is not None
        # Negative control: an explicit `load: false` still switches it off.
        sharedConfig["engine"]["qadb"] = {"load": False}
        assert qa(FakeActor(sharedConfig, logger), "qa").load_cmd(42) is None

    def test_the_default_without_a_db_qadb_does_not_load_or_fail(self, sharedConfig, logger, drpQaDir):
        del sharedConfig["engine"]["qadb"]
        del sharedConfig["db"]["qadb"]
        ctrl = qa(FakeActor(sharedConfig, logger), "qa")
        assert not ctrl.qadb_load and ctrl.load_cmd(42) is None

    def test_loading_without_a_db_qadb_fails_at_construction(self, sharedConfig, logger, drpQaDir):
        del sharedConfig["db"]["qadb"]
        with pytest.raises(RuntimeError, match=r"db\.qadb"):
            qa(FakeActor(sharedConfig, logger), "qa")

    def test_connects_where_the_shared_config_says(self, loading):
        assert loading.qadb == DB["qadb"] and loading.opdb_host == "db-ics"

    def test_the_loader_path_is_expanded_against_the_environment(self, loading):
        assert loading.loader_path == "/opt/drp_qa/bin.src/drpQa.py"

    def test_the_override_names_the_script_the_actor_adds_load(self, sharedConfig, logger, drpQaDir):
        sharedConfig["engine"]["qadb"]["loader"] = "$DRP_QA_DIR/../other/bin.src/drpQa.py"
        cmd = qa(FakeActor(sharedConfig, logger), "qa").load_cmd(42)
        assert cmd[:4] == ["python", "/opt/drp_qa/../other/bin.src/drpQa.py", "load", "-b"]

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
        assert "qadb: not loading (the config has no db.qadb)" in caplog.text

    def test_start_says_when_it_is_switched_off(self, sharedConfig, logger, drpQaDir, caplog):
        sharedConfig["engine"]["qadb"] = {"load": False}
        ctrl = qa(FakeActor(sharedConfig, logger), "qa")
        ctrl.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            ctrl.start()
        ctrl.stop()
        ctrl.join(timeout=5)
        assert "qadb: not loading (switched off)" in caplog.text


class TestSetQadbLoad:
    def test_switching_off_and_on_again(self, loading, popens):
        calls = popens(0)
        loading.set_qadb_load(False)
        assert loading.load_cmd(42) is None
        loading.load_qadb(42)
        assert calls == []

        loading.set_qadb_load(True)
        loading.load_qadb(42)
        assert calls == [loading.load_cmd(42)]

    def test_switching_on_without_a_db_qadb_raises_and_stays_off(self, controller):
        with pytest.raises(RuntimeError, match=r"db\.qadb"):
            controller.set_qadb_load(True)
        assert not controller.qadb_load

    def test_switching_on_with_an_unset_loader_var_raises_and_stays_off(self, sharedConfig, logger, drpQaDir):
        sharedConfig["engine"]["qadb"] = {"load": False, "loader": "$QA_TEST_UNSET_VAR/drpQa.py"}
        ctrl = qa(FakeActor(sharedConfig, logger), "qa")
        with pytest.raises(RuntimeError, match="QA_TEST_UNSET_VAR"):
            ctrl.set_qadb_load(True)
        assert not ctrl.qadb_load

    def test_switching_off_during_a_pipetask_skips_that_visits_load(self, loading, popens):
        calls = popens()
        loading.run_pipetask = lambda visitId: loading.set_qadb_load(False)
        loading.enqueue_visit(42)
        loading.stop()
        loading.run()
        # No load, and no second call in its place: the loader judges twilights itself.
        assert calls == []


#: The command line `load_cmd(42)` must build for `loading`, exactly (drp_qa's `drpQa.py load`).
EXPECTED_LOAD = [
    "python", "/opt/drp_qa/bin.src/drpQa.py", "load",
    "-b", "/work/datastore",
    "-c", "qaActor/reductions",
    "--visits", "42",
    "--mode", "realtime",
    "--host", "qadb-host",
    "--port", "5436",
    "--user", "pfs",
    "--dbname", "qadb",
    "--opdb-host", "db-ics",
    "--opdb-port", "5432",
    "--opdb-user", "pfs",
    "--opdb-dbname", "opdb",
]  # fmt: skip


class TestLoadCmd:
    def test_builds_the_exact_command_line(self, loading, sharedConfig, logger):
        assert loading.load_cmd(42) == EXPECTED_LOAD
        # Negative controls: the command of before, the removed loadQadb.py with no subcommand, is not
        # what is built, and neither is drpQa.py without `load`.
        old = ["python", "/opt/drp_qa/bin.src/loadQadb.py", *EXPECTED_LOAD[3:]]
        assert loading.load_cmd(42) != old
        assert loading.load_cmd(42) != [arg for arg in EXPECTED_LOAD if arg != "load"]
        # A config still naming loadQadb.py builds the old path, which the exact comparison rejects.
        sharedConfig["engine"]["qadb"]["loader"] = "$DRP_QA_DIR/bin.src/loadQadb.py"
        stale = qa(FakeActor(sharedConfig, logger), "qa").load_cmd(42)
        assert stale[1] == "/opt/drp_qa/bin.src/loadQadb.py" and stale != EXPECTED_LOAD

    def test_the_opdb_account_follows_the_config(self, sharedConfig, logger, drpQaDir):
        sharedConfig["db"]["opdb"] = {
            "host": "pfsa-db", "port": 5433, "user": "reader", "dbname": "opdb_copy",
        }  # fmt: skip
        cmd = qa(FakeActor(sharedConfig, logger), "qa").load_cmd(42)
        assert cmd[cmd.index("--opdb-host") :] == [
            "--opdb-host", "pfsa-db", "--opdb-port", "5433",
            "--opdb-user", "reader", "--opdb-dbname", "opdb_copy",
        ]  # fmt: skip

    def test_an_opdb_host_alone_takes_the_loaders_defaults(self, sharedConfig, logger, drpQaDir):
        sharedConfig["db"]["opdb"] = {"host": "pfsa-db"}
        cmd = qa(FakeActor(sharedConfig, logger), "qa").load_cmd(42)
        assert cmd[cmd.index("--opdb-host") :] == [
            "--opdb-host", "pfsa-db", "--opdb-port", "5432",
            "--opdb-user", "public_user", "--opdb-dbname", "opdb",
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
        calls = popens(0, 0, 0, 0, 0, 0)
        loading.enqueue_visit(1)
        loading.enqueue_visit(2)
        loading.stop()
        loading.run()
        # pipetask, the chain, then the loader, which judges twilights too.
        assert [cmd[0] for cmd in calls] == ["pipetask", "python", "python"] * 2
        loads = [cmd for cmd in calls if "--visits" in cmd]
        assert [cmd[cmd.index("--visits") + 1] for cmd in loads] == ["1", "2"]
        assert [calls.index(cmd) for cmd in loads] == [2, 5]

    def test_a_failed_pipetask_is_still_loaded(self, loading, popens):
        # pipetask exits non-zero when any quantum fails; the other images' verdicts are in the Butler.
        calls = popens(3, 0, 0)
        loading.enqueue_visit(1)
        loading.stop()
        loading.run()
        assert [cmd[0] for cmd in calls] == ["pipetask", "python", "python"]
        assert "--visits" in calls[2]

    def test_a_failed_load_does_not_stop_the_loop(self, loading, popens, caplog):
        calls = popens(0, 0, 1, 0, 0, 0)
        loading.enqueue_visit(1)
        loading.enqueue_visit(2)
        loading.stop()
        with caplog.at_level(logging.INFO):
            loading.run()
        assert len(calls) == 6
        assert (
            "qadb load failed for visit_id=1" in caplog.text and "qadb loaded for visit_id=2" in caplog.text
        )


class TestMarkReduceFailed:
    """Recording a visit's reduceExposure failure in qadb (REDUCE FAILED), so the dashboard shows it."""

    def test_builds_the_markReduceFailed_command(self, loading):
        cmd = loading.mark_failed_cmd(42, 1, "FAILED")
        assert cmd[:3] == ["python", "/opt/drp_qa/bin.src/drpQa.py", "markReduceFailed"]
        assert cmd[cmd.index("-c") + 1] == loading.output_collection
        assert cmd[cmd.index("--visit") + 1] == "42"
        assert cmd[cmd.index("--return-code") + 1] == "1"
        assert cmd[cmd.index("--status") + 1] == "FAILED"
        # The RUN is one of this actor's own (new_run prefixes the output collection), kept apart from a
        # real reduction's.
        assert cmd[cmd.index("--run") + 1].startswith(loading.output_collection + "/")
        # The opdb options follow the config: the visit's exposed cameras are read from it.
        assert cmd[cmd.index("--opdb-host") :] == [
            "--opdb-host", "db-ics", "--opdb-port", "5432",
            "--opdb-user", "pfs", "--opdb-dbname", "opdb",
        ]  # fmt: skip
        assert all(isinstance(arg, str) for arg in cmd)

    def test_omits_the_optional_return_code_and_status(self, loading):
        cmd = loading.mark_failed_cmd(42)
        assert "--return-code" not in cmd and "--status" not in cmd
        # Negative control: given, they are included.
        assert "--return-code" in loading.mark_failed_cmd(42, 1, "FAILED")

    def test_without_an_opdb_there_is_nothing_to_mark(self, sharedConfig, logger, drpQaDir):
        # No opdb means the exposed cameras cannot be read, so no verdict rows can be written.
        del sharedConfig["db"]["opdb"]
        sharedConfig["db"]["qadb"] = {"host": "qadb-host"}
        assert qa(FakeActor(sharedConfig, logger), "qa").mark_failed_cmd(42, 1, "FAILED") is None

    def test_without_qadb_loading_there_is_nothing_to_mark(self, controller):
        # The `controller` fixture has no db.qadb, so loading is off.
        assert controller.mark_failed_cmd(42, 1, "FAILED") is None

    def test_runs_the_command_and_logs(self, loading, popens, caplog):
        calls = popens(0)
        with caplog.at_level(logging.INFO):
            loading.mark_reduce_failed(42, 1, "FAILED")
        assert len(calls) == 1 and calls[0][2] == "markReduceFailed"
        assert "Marked 42 REDUCE FAILED in qadb" in caplog.text

    def test_warns_when_it_cannot_run(self, controller, popens, caplog):
        calls = popens(0)
        with caplog.at_level(logging.INFO):
            controller.mark_reduce_failed(42, 1, "FAILED")
        assert calls == []  # nothing is run when there is no qadb/opdb
        assert "Not marking 42 REDUCE FAILED" in caplog.text

    def test_warns_on_a_subprocess_failure(self, loading, popens, caplog):
        popens(2)
        with caplog.at_level(logging.INFO):
            loading.mark_reduce_failed(42, 1, "FAILED")
        assert "Marking 42 REDUCE FAILED failed (returncode 2)" in caplog.text

    def test_a_queued_mark_failed_item_runs_the_marker(self, loading, popens, caplog):
        calls = popens(0)
        loading.processing_queue.put(MarkFailed(42, 1, "FAILED"))
        loading.stop()
        with caplog.at_level(logging.INFO):
            loading.run()
        assert len(calls) == 1 and calls[0][2] == "markReduceFailed"
        assert cmd_has(calls[0], "--visit", "42")


def cmd_has(cmd, flag, value):
    """Whether ``cmd`` carries ``flag`` immediately followed by ``value``."""
    return flag in cmd and cmd[cmd.index(flag) + 1] == value
