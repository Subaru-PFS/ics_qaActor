"""Tests for the `qa` controller (qaActor/Controllers/qa.py).

This module is pure stdlib, so everything except `pipetask` itself is exercised
for real. `pipetask` is either replaced by a fake Popen or, where the point is to
test the subprocess plumbing, by a short Python program.
"""

import logging
import queue
import subprocess
import sys
import threading
import time

import pytest

from qaActor.Controllers.qa import DEFAULT_TIMEOUT, qa

from .conftest import FakeActor


class FakePopen:
    """A stand-in for `subprocess.Popen` covering only what `run_pipetask` uses.

    `returncode` stays None until `wait()` runs, which is what catches the code
    reading the exit status before the process has actually finished. It is a
    context manager because the real Popen is used as one, so that the stdout
    pipe is closed and the child reaped even if the read loop raises.
    """

    def __init__(self, lines=(), exitCode=0):
        self.stdout = iter(lines)
        self.returncode = None
        self.waited = False
        self.killed = False
        self.closed = False
        self._exitCode = exitCode

    def wait(self):
        self.waited = True
        if self.returncode is None:
            self.returncode = self._exitCode
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        # A killed child's pipe reaches EOF, which is what unblocks the reader.
        self.stdout = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        self.wait()
        return False


@pytest.fixture
def fakePopen(monkeypatch):
    """Capture the Popen call and hand back a canned process."""
    calls = []

    def factory(lines=(), exitCode=0):
        def fake(cmd, **kwargs):
            proc = FakePopen(lines, exitCode)
            calls.append({"cmd": cmd, "kwargs": kwargs, "proc": proc})
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake)
        return calls

    return factory


# ------------------------------------------------------------------------------
# Construction / configuration
# ------------------------------------------------------------------------------
class TestInit:
    def test_reads_butler_config(self, controller):
        assert controller.datastore == "/work/datastore"
        assert controller.output_collection == "qaActor/reductions"

    def test_joins_input_collection_list_into_one_comma_separated_arg(self, controller):
        # pipetask -i takes a single comma-separated value, but the YAML lists them.
        assert controller.input_collections == "PFS/calib/2024,drpActor/reductions,PFS/defaults"

    def test_accepts_input_collections_already_given_as_a_string(self, actorConfig, logger, drpQaDir):
        actorConfig["engine"]["butler"]["input"] = "a,b"
        ctrl = qa(FakeActor(actorConfig, logger), "qa")
        assert ctrl.input_collections == "a,b"

    def test_expands_pipeline_path_against_the_environment(self, controller):
        assert controller.pipeline_path == "/opt/drp_qa/pipelines/drpQA.yaml"

    def test_unset_pipeline_env_var_is_left_unexpanded(self, actorConfig, logger, monkeypatch):
        # Documents a sharp edge: os.path.expandvars leaves an unset variable in
        # place rather than raising, so a missing DRP_QA_DIR only surfaces later
        # as a pipetask failure.
        monkeypatch.delenv("DRP_QA_DIR", raising=False)
        ctrl = qa(FakeActor(actorConfig, logger), "qa")
        assert ctrl.pipeline_path == "$DRP_QA_DIR/pipelines/drpQA.yaml"

    def test_num_procs_comes_from_config(self, controller):
        assert controller.num_procs == 4

    def test_num_procs_defaults_to_8(self, actorConfig, logger, drpQaDir):
        del actorConfig["engine"]["num_procs"]
        ctrl = qa(FakeActor(actorConfig, logger), "qa")
        assert ctrl.num_procs == 8

    def test_is_a_named_daemon_thread(self, controller):
        assert isinstance(controller, threading.Thread)
        assert controller.daemon is True
        assert controller.name == "qa"

    def test_starts_with_an_empty_queue(self, controller):
        assert isinstance(controller.processing_queue, queue.Queue)
        assert controller.queue_size() == 0

    def test_starts_with_no_visit_in_flight(self, controller):
        assert controller.current_visit is None

    def test_sets_the_requested_log_level_on_its_own_logger(self, actor, drpQaDir):
        ctrl = qa(actor, "qa", logLevel=logging.WARNING)
        assert ctrl.logger.level == logging.WARNING

    def test_defaults_to_debug_log_level(self, actor, drpQaDir):
        ctrl = qa(actor, "qa")
        assert ctrl.logger.level == logging.DEBUG

    def test_logs_to_a_child_of_the_actor_logger(self, actor, drpQaDir):
        ctrl = qa(actor, "qa")
        assert ctrl.logger.name == f"{actor.logger.name}.qa"
        assert ctrl.logger.parent is actor.logger

    def test_does_not_relevel_the_actors_own_logger(self, actor, drpQaDir):
        # `actor.logger` is the shared `actor` logger, levelled from
        # logging.baseLevel in the config. Attaching a controller must not
        # silently drag the whole actor down to DEBUG.
        actor.logger.setLevel(logging.INFO)
        qa(actor, "qa", logLevel=logging.DEBUG)
        assert actor.logger.level == logging.INFO

    def test_timeout_comes_from_config(self, actorConfig, logger, drpQaDir):
        actorConfig["engine"]["timeout"] = 120
        assert qa(FakeActor(actorConfig, logger), "qa").timeout == 120

    def test_timeout_defaults_to_ten_minutes(self, controller):
        assert controller.timeout == DEFAULT_TIMEOUT == 600

    def test_construction_matches_how_ICC_attachController_calls_it(self, actor, drpQaDir):
        # ICC does `controllerClass(self, instanceName)` positionally, then
        # `conn.start(cmd=cmd)`, and detach does `controller.stop(cmd=cmd)`.
        # This pins that seam, which nothing else here would catch.
        ctrl = qa(actor, "qa")
        assert ctrl.name == "qa"
        assert callable(ctrl.start) and callable(ctrl.stop)


# ------------------------------------------------------------------------------
# Command construction
# ------------------------------------------------------------------------------
class TestPipetaskCmd:
    def test_builds_the_exact_command_line(self, controller):
        assert controller.pipetask_cmd(12345) == [
            "pipetask",
            "--long-log",
            "--log-level",
            ".=INFO",
            "run",
            "-j",
            "4",
            "-b",
            "/work/datastore",
            "-i",
            "PFS/calib/2024,drpActor/reductions,PFS/defaults",
            "-o",
            "qaActor/reductions",
            "-p",
            "/opt/drp_qa/pipelines/drpQA.yaml",
            "-d",
            "visit = 12345",
        ]

    def test_num_procs_is_stringified_for_the_j_flag(self, controller):
        cmdLine = controller.pipetask_cmd(1)
        assert cmdLine[cmdLine.index("-j") + 1] == "4"

    def test_visit_is_passed_as_a_data_query(self, controller):
        cmdLine = controller.pipetask_cmd(98765)
        assert cmdLine[cmdLine.index("-d") + 1] == "visit = 98765"

    def test_every_argument_is_a_string(self, controller):
        # Popen with a non-str element raises, so this would be a runtime failure.
        assert all(isinstance(arg, str) for arg in controller.pipetask_cmd(1))


# ------------------------------------------------------------------------------
# Running the pipeline
# ------------------------------------------------------------------------------
class TestRunPipetask:
    def test_invokes_popen_with_merged_piped_text_output(self, controller, fakePopen):
        calls = fakePopen()
        controller.run_pipetask(42)

        assert len(calls) == 1
        assert calls[0]["cmd"] == controller.pipetask_cmd(42)
        assert calls[0]["kwargs"] == {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
        }

    def test_relays_pipetask_output_to_the_log_without_trailing_newlines(self, controller, fakePopen, caplog):
        fakePopen(lines=["first line\n", "second line\n"])
        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        assert "first line" in caplog.messages
        assert "second line" in caplog.messages

    def test_waits_for_the_process_before_reading_the_exit_status(self, controller, fakePopen):
        calls = fakePopen()
        controller.run_pipetask(42)
        assert calls[0]["proc"].waited is True

    def test_logs_success_on_a_zero_exit(self, controller, fakePopen, caplog):
        fakePopen(exitCode=0)
        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        assert "QA complete for visit_id=42" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_warns_on_a_nonzero_exit(self, controller, fakePopen, caplog):
        fakePopen(exitCode=3)
        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        assert "QA pipetask failed for visit_id=42 (returncode 3)" in caplog.text
        assert [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_a_pipetask_failure_does_not_raise(self, controller, fakePopen):
        # The failure is reported through the log, not an exception; the run loop
        # depends on that to keep going.
        fakePopen(exitCode=1)
        controller.run_pipetask(42)

    def test_logs_the_command_it_is_about_to_run(self, controller, fakePopen, caplog):
        fakePopen()
        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        assert "Running: pipetask --long-log" in caplog.text

    @pytest.mark.parametrize(("exitCode", "expected"), [(0, "QA complete"), (7, "returncode 7")])
    def test_against_a_real_subprocess(self, controller, monkeypatch, caplog, exitCode, expected):
        """Exercise the actual Popen/stdout/wait path, standing in for pipetask."""
        program = f"import sys; print('hello from the pipeline'); sys.exit({exitCode})"
        monkeypatch.setattr(controller, "pipetask_cmd", lambda visitId: [sys.executable, "-c", program])

        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        assert "hello from the pipeline" in caplog.messages
        assert expected in caplog.text


class TestPipetaskResourceHandling:
    """The child process is context-managed so its pipe is not left to the GC."""

    def test_popen_is_used_as_a_context_manager(self, controller, fakePopen):
        calls = fakePopen()
        controller.run_pipetask(42)
        assert calls[0]["proc"].closed is True

    def test_the_process_is_closed_even_when_the_read_loop_raises(self, controller, fakePopen, monkeypatch):
        calls = fakePopen(lines=["a line\n"])
        monkeypatch.setattr(controller.logger, "info", _raiseOn("a line", RuntimeError("logging blew up")))

        with pytest.raises(RuntimeError):
            controller.run_pipetask(42)

        assert calls[0]["proc"].closed is True, "the stdout pipe must not be leaked on the error path"

    def test_a_real_subprocess_leaves_no_open_pipe(self, controller, monkeypatch):
        program = "print('done')"
        monkeypatch.setattr(controller, "pipetask_cmd", lambda visitId: [sys.executable, "-c", program])

        opened = []
        realPopen = subprocess.Popen
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: opened.append(realPopen(*a, **kw)) or opened[-1]
        )

        controller.run_pipetask(42)

        assert opened[0].stdout.closed, "Popen.__exit__ must close the stdout pipe"


class TestPipetaskTimeout:
    """A hung pipetask would otherwise block the actor's only QA consumer forever."""

    def test_no_watchdog_is_armed_when_the_timeout_is_disabled(self, controller, fakePopen):
        controller.timeout = 0
        calls = fakePopen()
        controller.run_pipetask(42)
        assert calls[0]["proc"].killed is False

    def test_a_hung_pipeline_is_killed_and_reported(self, controller, monkeypatch, caplog):
        # Sleeps far longer than the timeout and never writes a line, so the read
        # loop is blocked: only the watchdog can end this.
        program = "import time; time.sleep(30)"
        monkeypatch.setattr(controller, "pipetask_cmd", lambda visitId: [sys.executable, "-c", program])
        controller.timeout = 0.5

        started = time.monotonic()
        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)
        elapsed = time.monotonic() - started

        assert elapsed < 20, "the watchdog must not wait for the child to finish on its own"
        assert "QA pipetask timed out for visit_id=42 after 0.5s, killed" in caplog.text

    def test_a_timeout_is_reported_as_a_timeout_not_a_plain_failure(self, controller, monkeypatch, caplog):
        program = "import time; time.sleep(30)"
        monkeypatch.setattr(controller, "pipetask_cmd", lambda visitId: [sys.executable, "-c", program])
        controller.timeout = 0.5

        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        # A killed child exits with -9; saying only "returncode -9" would send an
        # operator hunting for a pipeline bug that is not there.
        assert "QA pipetask failed" not in caplog.text
        assert "QA complete" not in caplog.text

    def test_the_watchdog_is_cancelled_when_the_pipeline_finishes_in_time(
        self, controller, monkeypatch, caplog
    ):
        program = "print('quick')"
        monkeypatch.setattr(controller, "pipetask_cmd", lambda visitId: [sys.executable, "-c", program])
        controller.timeout = 30

        timers = []
        realTimer = threading.Timer
        monkeypatch.setattr(
            threading, "Timer", lambda *a, **kw: timers.append(realTimer(*a, **kw)) or timers[-1]
        )

        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        assert "QA complete for visit_id=42" in caplog.text
        # `cancel` retires the timer by setting its finished event; the thread
        # itself winds down a moment later, so assert on the event, not is_alive.
        assert timers[0].finished.is_set(), "an uncancelled watchdog would linger for the full timeout"

    def test_the_watchdog_does_not_block_interpreter_shutdown(self, controller, fakePopen, monkeypatch):
        # A non-daemon timer would hold the process open for the whole timeout.
        controller.timeout = 30
        fakePopen()

        timers = []
        realTimer = threading.Timer
        monkeypatch.setattr(
            threading, "Timer", lambda *a, **kw: timers.append(realTimer(*a, **kw)) or timers[-1]
        )

        controller.run_pipetask(42)

        assert timers[0].daemon is True

    def test_a_watchdog_firing_on_a_clean_exit_is_not_called_a_timeout(self, controller, fakePopen, caplog):
        # The watchdog can fire in the moment between the read loop ending and
        # the timer being cancelled. If the pipeline exited zero it did finish in
        # time, and saying otherwise would send someone chasing a phantom hang.
        fakePopen(exitCode=0)

        def fireImmediately(process, visitId, timedOut):
            timedOut.set()
            return None

        controller._start_watchdog = fireImmediately

        with caplog.at_level(logging.INFO):
            controller.run_pipetask(42)

        assert "QA complete for visit_id=42" in caplog.text
        assert "timed out" not in caplog.text

    def test_a_timed_out_visit_does_not_stop_the_loop(self, controller, monkeypatch, caplog):
        program = "import time; time.sleep(30)"
        monkeypatch.setattr(controller, "pipetask_cmd", lambda visitId: [sys.executable, "-c", program])
        controller.timeout = 0.5

        controller.enqueue_visit(1)
        controller.stop()

        with caplog.at_level(logging.INFO):
            controller.run()

        assert "Received the stop sentinel" in caplog.text


def _raiseOn(needle, error):
    """Build a logger.info stand-in that raises the first time it sees `needle`."""

    def info(msg, *args, **kwargs):
        if needle in str(msg):
            raise error

    return info


# ------------------------------------------------------------------------------
# Queue API
# ------------------------------------------------------------------------------
class TestQueueApi:
    def test_enqueue_visit_makes_the_visit_available(self, controller):
        controller.enqueue_visit(101)
        assert controller.processing_queue.get_nowait() == 101

    def test_queue_size_tracks_enqueued_visits(self, controller):
        assert controller.queue_size() == 0
        controller.enqueue_visit(1)
        controller.enqueue_visit(2)
        assert controller.queue_size() == 2

    def test_visits_are_processed_first_in_first_out(self, controller):
        for visitId in (1, 2, 3):
            controller.enqueue_visit(visitId)
        drained = [controller.processing_queue.get_nowait() for _ in range(3)]
        assert drained == [1, 2, 3]


# ------------------------------------------------------------------------------
# The consumer loop, driven directly (no thread)
# ------------------------------------------------------------------------------
class TestRunLoop:
    def test_processes_each_queued_visit_in_order_then_stops_on_the_sentinel(self, controller):
        processed = []
        controller.run_pipetask = processed.append

        controller.enqueue_visit(1)
        controller.enqueue_visit(2)
        controller.stop()

        controller.run()

        assert processed == [1, 2]

    def test_exits_immediately_on_the_sentinel(self, controller, caplog):
        controller.run_pipetask = lambda visitId: pytest.fail("should not process the sentinel")
        controller.stop()

        with caplog.at_level(logging.INFO):
            controller.run()

        assert "Received the stop sentinel" in caplog.text

    def test_a_failing_visit_is_logged_and_the_loop_carries_on(self, controller, caplog):
        seen = []

        def flaky(visitId):
            seen.append(visitId)
            if visitId == 1:
                raise RuntimeError("pipetask exploded")

        controller.run_pipetask = flaky

        controller.enqueue_visit(1)
        controller.enqueue_visit(2)
        controller.stop()

        with caplog.at_level(logging.INFO):
            controller.run()

        assert seen == [1, 2], "a failed visit must not stop later visits"
        assert "Error processing visit_id=1: pipetask exploded" in caplog.text

    def test_tracks_the_visit_while_it_is_being_processed(self, controller):
        seen = []
        controller.run_pipetask = lambda visitId: seen.append(controller.current_visit)

        controller.enqueue_visit(4242)
        controller.stop()

        controller.run()

        assert seen == [4242], "current_visit must be set for the duration of the run"

    def test_clears_the_current_visit_when_the_visit_finishes(self, controller):
        controller.run_pipetask = lambda visitId: None

        controller.enqueue_visit(4242)
        controller.stop()

        controller.run()

        assert controller.current_visit is None

    def test_clears_the_current_visit_when_the_visit_fails(self, controller, caplog):
        def boom(visitId):
            raise RuntimeError("pipetask exploded")

        controller.run_pipetask = boom

        controller.enqueue_visit(4242)
        controller.stop()

        with caplog.at_level(logging.INFO):
            controller.run()

        assert controller.current_visit is None, "a failed visit must not stay in flight"

    def test_the_sentinel_is_never_reported_as_the_current_visit(self, controller):
        controller.run_pipetask = lambda visitId: None
        controller.stop()

        controller.run()

        assert controller.current_visit is None

    def test_leaves_nothing_behind_in_the_queue(self, controller):
        controller.run_pipetask = lambda visitId: None
        controller.enqueue_visit(1)
        controller.stop()

        controller.run()

        assert controller.queue_size() == 0


# ------------------------------------------------------------------------------
# Lifecycle hooks
# ------------------------------------------------------------------------------
class TestLifecycle:
    def test_start_logs_the_resolved_configuration(self, controller, caplog):
        controller.run_pipetask = lambda visitId: None
        with caplog.at_level(logging.INFO):
            controller.start()

        assert "Pipeline path: /opt/drp_qa/pipelines/drpQA.yaml" in caplog.text
        assert "Datastore: /work/datastore" in caplog.text
        assert "Input collections: PFS/calib/2024,drpActor/reductions,PFS/defaults" in caplog.text
        assert "Output collection: qaActor/reductions" in caplog.text

    def test_start_reports_to_the_commander_when_given_one(self, controller, cmd):
        controller.run_pipetask = lambda visitId: None
        controller.start(cmd=cmd)
        assert 'text="QA processing loop started"' in cmd.informs

    def test_start_works_without_a_commander(self, controller):
        controller.run_pipetask = lambda visitId: None
        controller.start()
        assert controller.is_alive()

    def test_stop_reports_to_the_commander_when_given_one(self, controller, cmd):
        controller.stop(cmd=cmd)
        assert 'text="QA processing loop stopped"' in cmd.informs

    def test_stop_enqueues_the_shutdown_sentinel(self, controller):
        controller.stop()
        assert controller.processing_queue.get_nowait() is None

    def test_stop_is_safe_before_the_thread_ever_started(self, controller):
        controller.stop()  # must not raise
        assert controller.queue_size() == 1

    def test_the_sentinel_goes_to_the_back_of_the_queue(self, controller):
        # Documents that stop() is cooperative, not a cancel: visits already
        # queued are processed before the loop sees the sentinel.
        controller.enqueue_visit(1)
        controller.stop()
        assert list(controller.processing_queue.queue) == [1, None]

    def test_start_then_enqueue_then_stop_round_trip(self, controller, cmd):
        """The whole lifecycle on a real thread, as the actor drives it."""
        processed = []
        firstVisitDone = threading.Event()

        def record(visitId):
            processed.append(visitId)
            firstVisitDone.set()

        controller.run_pipetask = record

        controller.start(cmd=cmd)
        assert controller.is_alive()

        controller.enqueue_visit(4242)
        assert firstVisitDone.wait(timeout=5), "visit was never picked up off the queue"

        controller.stop(cmd=cmd)
        controller.join(timeout=5)

        assert not controller.is_alive()
        assert processed == [4242]

    def test_a_started_loop_idles_instead_of_spinning(self, controller):
        # The loop must block on queue.get rather than busy-wait; if it were
        # spinning it would burn through the empty queue and exit.
        controller.run_pipetask = lambda visitId: None
        controller.start()
        controller.join(timeout=0.2)
        assert controller.is_alive()
