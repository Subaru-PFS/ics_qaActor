"""Tests for judging each visit's twilight sequence after its load (INSTRM-3089).

The judge is drp_qa's `bin.src/twilightQa.py`, run as a subprocess the way the loader is; its
exit status says what happened to the visit's sequence. It is replaced by a fake Popen, or by a
short Python program where the point is the watchdog.
"""

import logging
import sys

import pytest

from qaActor.Controllers.qa import (
    TWILIGHT_JUDGED,
    TWILIGHT_NOT_TWILIGHT,
    TWILIGHT_NOT_WRITTEN,
    TWILIGHT_PENDING,
    TWILIGHT_TIMEOUT,
    TwilightCheck,
    qa,
)

from .conftest import FakeActor
from .test_qa_controller import fakePopen  # noqa: F401 — fixture
from .test_qa_qadb import loading, popens, sharedConfig  # noqa: F401 — fixtures


@pytest.fixture
def judging(loading):  # noqa: F811
    """A controller with an opdb and qadb loading on: judging into qadb."""
    return loading


class TestConfig:
    def test_without_a_db_opdb_nothing_is_judged(self, controller, popens):  # noqa: F811
        calls = popens(0)
        assert controller.twilight_cmd(42) is None
        controller.judge_twilight(42)
        assert calls == []
        assert controller.describe_twilight() == "twilight: not judging sequences (the config has no db.opdb)"

    def test_the_judge_path_is_expanded_against_the_environment(self, judging):
        assert judging.twilight_path == "/opt/drp_qa/bin.src/twilightQa.py"

    def test_an_unset_env_var_fails_at_construction(self, sharedConfig, logger, monkeypatch):  # noqa: F811
        # The pipeline's own check would catch an unset DRP_QA_DIR first; a literal pipeline path
        # gets past it, and the judge's check must then speak up itself.
        monkeypatch.delenv("DRP_QA_DIR", raising=False)
        sharedConfig["engine"]["pipeline"] = "/opt/pipelines/drpQA.yaml"
        del sharedConfig["engine"]["qadb"]
        del sharedConfig["db"]["qadb"]
        with pytest.raises(RuntimeError, match=r"twilight judge path .*DRP_QA_DIR"):
            qa(FakeActor(sharedConfig, logger), "qa")
        # Negative control: without a db.opdb the judge never runs, so the unset variable is no failure.
        del sharedConfig["db"]["opdb"]
        assert qa(FakeActor(sharedConfig, logger), "qa").twilight_cmd(42) is None

    def test_the_timeout_is_its_own(self, judging):
        assert judging.twilight_timeout == TWILIGHT_TIMEOUT
        assert judging.twilight_timeout != judging.timeout

    def test_start_says_where_it_judges(self, judging, caplog):
        judging.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            judging.start()
        assert (
            "twilight: judging each visit's sequence with /opt/drp_qa/bin.src/twilightQa.py, opdb db-ics,"
            " verdicts into qadb at qadb-host:5436" in caplog.text
        )

    def test_start_says_when_the_verdicts_go_to_the_log_alone(self, judging, caplog):
        judging.set_qadb_load(False)
        judging.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            judging.start()
        assert "verdicts to the log alone (qadb loading is off)" in caplog.text

    def test_start_says_when_it_does_not_judge(self, controller, caplog):
        controller.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            controller.start()
        assert "twilight: not judging sequences (the config has no db.opdb)" in caplog.text


class TestTwilightCmd:
    def test_builds_the_exact_command_line(self, judging):
        assert judging.twilight_cmd(42) == [
            "python", "/opt/drp_qa/bin.src/twilightQa.py",
            "-b", "/work/datastore",
            "-c", "qaActor/reductions",
            "--sequence-of-visit", "42",
            "--opdb-host", "db-ics",
            "--qadb-host", "qadb-host",
            "--qadb-port", "5436",
            "--qadb-user", "pfs",
            "--qadb-dbname", "qadb",
        ]  # fmt: skip

    def test_reads_the_collection_the_pipeline_writes(self, judging):
        cmd = judging.twilight_cmd(42)
        assert cmd[cmd.index("-c") + 1] == judging.output_collection

    def test_with_loading_off_the_qadb_is_left_out(self, judging):
        judging.set_qadb_load(False)
        cmd = judging.twilight_cmd(42)
        assert "--qadb-host" not in cmd
        assert cmd[-2:] == ["--opdb-host", "db-ics"]
        # Negative control: switched back on, the qadb returns.
        judging.set_qadb_load(True)
        assert "--qadb-host" in judging.twilight_cmd(42)

    def test_without_a_db_qadb_the_qadb_is_left_out(self, sharedConfig, logger, drpQaDir):  # noqa: F811
        del sharedConfig["db"]["qadb"]
        del sharedConfig["engine"]["qadb"]
        cmd = qa(FakeActor(sharedConfig, logger), "qa").twilight_cmd(42)
        assert cmd is not None and "--qadb-host" not in cmd

    def test_the_qadb_options_follow_the_shared_config(self, sharedConfig, logger, drpQaDir):  # noqa: F811
        sharedConfig["db"]["qadb"] = {"host": "other", "port": 5999, "user": "writer", "dbname": "qadb2"}
        cmd = qa(FakeActor(sharedConfig, logger), "qa").twilight_cmd(42)
        options = cmd[cmd.index("--qadb-host") + 1 :]
        assert options == ["other", "--qadb-port", "5999", "--qadb-user", "writer", "--qadb-dbname", "qadb2"]

    def test_every_argument_is_a_string(self, judging):
        assert all(isinstance(arg, str) for arg in judging.twilight_cmd(42))


class TestJudgeTwilight:
    """Each exit status of the judge gets its own log line; none raises."""

    def test_judged(self, judging, popens, caplog):  # noqa: F811
        calls = popens(TWILIGHT_JUDGED)
        with caplog.at_level(logging.INFO):
            judging.judge_twilight(42)
        assert calls == [judging.twilight_cmd(42)]
        assert "twilight sequence judged for visit_id=42" in caplog.text

    def test_pending(self, judging, popens, caplog):  # noqa: F811
        popens(TWILIGHT_PENDING)
        with caplog.at_level(logging.INFO):
            judging.judge_twilight(42)
        assert "twilight sequence of visit_id=42 not complete yet" in caplog.text
        assert caplog.records[-1].levelno == logging.INFO  # routine, not a warning

    def test_not_a_twilight_is_quiet(self, judging, popens, caplog):  # noqa: F811
        popens(TWILIGHT_NOT_TWILIGHT)
        with caplog.at_level(logging.INFO):
            judging.judge_twilight(42)
        told = [r for r in caplog.records if r.levelno >= logging.INFO and "twilight sequence" in r.message]
        assert told == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_not_written_warns_and_points_at_the_log(self, judging, popens, caplog):  # noqa: F811
        popens(TWILIGHT_NOT_WRITTEN)
        with caplog.at_level(logging.INFO):
            judging.judge_twilight(42)
        (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "judged but not written to qadb" in warning.message
        assert "qa twilight 42" in warning.message

    def test_any_other_status_is_a_failure(self, judging, popens, caplog):  # noqa: F811
        popens(1)
        with caplog.at_level(logging.INFO):
            judging.judge_twilight(42)
        assert "twilight judge failed for visit_id=42 (returncode 1)" in caplog.text
        assert "Failed command: python /opt/drp_qa/bin.src/twilightQa.py" in caplog.text

    def test_the_exit_statuses_are_distinct(self):
        assert len({TWILIGHT_JUDGED, TWILIGHT_PENDING, TWILIGHT_NOT_TWILIGHT, TWILIGHT_NOT_WRITTEN}) == 4
        assert TWILIGHT_JUDGED == 0

    def test_the_judges_output_is_relayed_to_the_log(self, judging, fakePopen, caplog):  # noqa: F811
        line = "Visit 42: sequence 71000 is waiting for 42 (no tables yet)\n"
        fakePopen(lines=[line], exitCode=TWILIGHT_PENDING)
        with caplog.at_level(logging.INFO):
            judging.judge_twilight(42)
        assert "Visit 42: sequence 71000 is waiting for 42 (no tables yet)" in caplog.text

    def test_a_hung_judge_is_killed_with_its_own_timeout(self, judging, monkeypatch, caplog):
        program = "import time; time.sleep(30)"
        monkeypatch.setattr(judging, "twilight_cmd", lambda visitId: [sys.executable, "-c", program])
        judging.twilight_timeout = 0.5
        judging.timeout = judging.load_timeout = 600  # the others', which must not apply here
        with caplog.at_level(logging.INFO):
            judging.judge_twilight(42)
        assert "twilight judge for visit_id=42 exceeded 0.5s, killing it" in caplog.text
        assert "twilight judge timed out for visit_id=42 after 0.5s, killed" in caplog.text


class TestRunLoop:
    def test_each_visit_is_judged_after_its_load(self, judging, popens):  # noqa: F811
        calls = popens(0, 0, 0, 0, 0, 0)
        judging.enqueue_visit(1)
        judging.enqueue_visit(2)
        judging.stop()
        judging.run()
        assert [cmd[1].rsplit("/", 1)[-1] if cmd[0] == "python" else cmd[0] for cmd in calls] == [
            "pipetask", "loadQadb.py", "twilightQa.py",
            "pipetask", "loadQadb.py", "twilightQa.py",
        ]  # fmt: skip
        assert [cmd[cmd.index("--sequence-of-visit") + 1] for cmd in calls[2::3]] == ["1", "2"]

    def test_a_failed_pipetask_and_load_are_still_judged(self, judging, popens):  # noqa: F811
        # The judge says what the sequence is waiting for, and a complete one is judged anyway.
        calls = popens(3, 1, TWILIGHT_PENDING)
        judging.enqueue_visit(1)
        judging.stop()
        judging.run()
        assert len(calls) == 3 and calls[2][1].endswith("twilightQa.py")

    def test_a_failed_judge_does_not_stop_the_loop(self, judging, popens, caplog):  # noqa: F811
        calls = popens(0, 0, 1, 0, 0, 0)
        judging.enqueue_visit(1)
        judging.enqueue_visit(2)
        judging.stop()
        with caplog.at_level(logging.INFO):
            judging.run()
        assert len(calls) == 6
        assert "twilight judge failed for visit_id=1" in caplog.text
        assert "twilight sequence judged for visit_id=2" in caplog.text

    def test_a_twilight_check_runs_the_judge_alone(self, judging, popens):  # noqa: F811
        calls = popens(TWILIGHT_JUDGED)
        judging.enqueue_twilight(42)
        judging.stop()
        judging.run()
        assert calls == [judging.twilight_cmd(42)]
        assert judging.current_visit is None

    def test_a_twilight_check_queues_behind_visits(self, judging, popens):  # noqa: F811
        calls = popens(0, 0, 0, 0)
        judging.enqueue_visit(1)
        judging.enqueue_twilight(2)
        judging.stop()
        judging.run()
        judged = [cmd[cmd.index("--sequence-of-visit") + 1] for cmd in calls if "--sequence-of-visit" in cmd]
        assert judged == ["1", "2"]
        assert len(calls) == 4

    def test_the_check_holds_a_plain_int(self, judging):
        judging.enqueue_twilight("42")
        item = judging.processing_queue.get_nowait()
        assert item == TwilightCheck(42) and type(item.visit) is int

    def test_without_a_db_opdb_a_visit_is_processed_without_a_judge(self, controller, popens):  # noqa: F811
        calls = popens(0, 0)
        controller.enqueue_visit(1)
        controller.stop()
        controller.run()
        assert [cmd[0] for cmd in calls] == ["pipetask"]
