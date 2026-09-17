"""Tests for the MHS command handlers (qaActor/Commands/QaCmd.py).

The commands are tested against the *real* `qa` controller rather than a mock,
so the command/controller seam (`queue_size`, `enqueue_visit`) is covered too.
"""

import opscore.protocols.types as types
import pytest

from qaActor.Commands.QaCmd import QaCmd

from .conftest import FakeCmd, FakeModel


class KeywordCmd(FakeCmd):
    """A FakeCmd that also carries parsed command keywords."""

    def __init__(self, **keywords):
        super().__init__()
        self.cmd = type("ParsedCmd", (), {"keywords": {k: _Values(v) for k, v in keywords.items()}})()


class _Values:
    def __init__(self, value):
        self.values = [value]


@pytest.fixture
def qaCmd(actor, controller):
    actor.controllers["qa"] = controller
    return QaCmd(actor)


class TestWiring:
    def test_exposes_the_documented_vocabulary(self, qaCmd):
        assert [(name, args) for name, args, _ in qaCmd.vocab] == [
            ("ping", ""),
            ("status", ""),
            ("show", ""),
            ("process", "<visit_id>"),
        ]

    def test_every_vocabulary_entry_points_at_a_handler(self, qaCmd):
        for name, _, handler in qaCmd.vocab:
            assert callable(handler), f"{name} has no handler"

    def test_declares_the_visit_id_key(self, qaCmd):
        assert "visit_id" in qaCmd.keys.keys
        assert isinstance(qaCmd.keys.keys["visit_id"].typedValues.vtypes[0], types.Int)

    def test_keys_dictionary_is_named_for_the_actor_and_module(self, qaCmd):
        # MHS looks the dictionary up as <actor>_<module>; a rename here silently
        # breaks command parsing at runtime.
        assert qaCmd.keys.name == "qa_qa"

    def test_resolves_the_qa_controller_from_the_actor(self, qaCmd, controller):
        assert qaCmd._get_controller() is controller


class TestPing:
    def test_reports_the_product_name_and_finishes(self, qaCmd, cmd):
        qaCmd.ping(cmd)
        assert cmd.informs == ['text="qaActor"']
        assert cmd.finished


class TestStatus:
    def test_reports_an_empty_queue_and_an_idle_loop(self, qaCmd, cmd):
        qaCmd.status(cmd)
        assert cmd.informs == [
            'text="QA currently processing: idle"',
            'text="QA processing queue size: 0"',
        ]
        assert cmd.finished

    def test_reports_the_live_queue_depth(self, qaCmd, cmd, controller):
        controller.enqueue_visit(1)
        controller.enqueue_visit(2)

        qaCmd.status(cmd)

        assert 'text="QA processing queue size: 2"' in cmd.informs

    def test_reports_the_visit_being_processed(self, qaCmd, cmd, controller):
        controller._current_visit = 12345

        qaCmd.status(cmd)

        assert cmd.informs == [
            'text="QA currently processing: 12345"',
            'text="QA processing queue size: 0"',
        ]
        assert cmd.finished

    def test_a_visit_in_flight_is_not_counted_in_the_queue_depth(self, qaCmd, cmd, controller):
        # The in-flight visit has already been taken off the queue, so the two
        # numbers are independent: one visit running, one still waiting.
        controller._current_visit = 12345
        controller.enqueue_visit(12346)

        qaCmd.status(cmd)

        assert cmd.informs == [
            'text="QA currently processing: 12345"',
            'text="QA processing queue size: 1"',
        ]


def _unescape(line):
    """Undo qstr's backslash escaping, to compare against the raw value."""
    return line.replace('\\"', '"')


class TestShow:
    def test_dumps_every_keyvar_from_every_model(self, qaCmd, cmd, actor):
        actor.models["drp"] = FakeModel({"reduceExposureStatus": "statusValue"})
        actor.models["sps"] = FakeModel({"exposureState": "stateValue"})

        qaCmd.show(cmd)

        assert cmd.informs == [
            "text=\"'statusValue'\"",
            "text=\"'stateValue'\"",
        ]
        assert cmd.finished

    def test_quotes_values_that_contain_a_double_quote(self, qaCmd, cmd, actor):
        """A repr with an inner double quote must not break out of text="...".

        Python flips a string's repr to double quotes as soon as it holds an
        apostrophe, so a keyvar carrying a message like `can't open file` is the
        realistic trigger — drp.reduceExposureStatus has a statusStr field.
        """
        import opscore.actor.keyvar as keyvar
        import opscore.protocols.keys as keys

        key = keys.Key("reduceExposureStatus", types.Int(), types.String())
        var = keyvar.KeyVar("drp", key)
        var.set(["1", "can't open file"])
        assert '"' in repr(var), "this test is pointless if the repr has no inner quote"

        actor.models["drp"] = FakeModel({"reduceExposureStatus": var})

        qaCmd.show(cmd)

        (line,) = cmd.informs
        assert line.startswith('text="') and line.endswith('"')
        # Every inner quote is escaped, so the value survives as one MHS token.
        assert '\\"' in line
        assert _unescape(line) == f'text="{var!r}"'

    def test_quotes_a_broken_models_error_message(self, qaCmd, cmd, actor):
        class BrokenModel:
            @property
            def keyVarDict(self):
                raise RuntimeError('it said "no"')

        actor.models["broken"] = BrokenModel()

        qaCmd.show(cmd)

        (warning,) = cmd.warns
        assert warning.startswith('text="') and warning.endswith('"')
        # The quotes inside the exception message must be escaped, not passed
        # through to close the value early.
        assert warning == r'text="QaCmd.show: broken: it said \"no\""'
        assert _unescape(warning) == 'text="QaCmd.show: broken: it said "no""'

    def test_finishes_cleanly_with_no_models(self, qaCmd, cmd):
        qaCmd.show(cmd)
        assert cmd.informs == []
        assert cmd.finished

    def test_a_broken_model_is_warned_about_and_the_rest_still_reported(self, qaCmd, cmd, actor):
        class BrokenModel:
            @property
            def keyVarDict(self):
                raise RuntimeError("model not connected")

        actor.models["broken"] = BrokenModel()
        actor.models["drp"] = FakeModel({"reduceExposureStatus": "statusValue"})

        qaCmd.show(cmd)

        assert cmd.warns == ['text="QaCmd.show: broken: model not connected"']
        assert cmd.informs == ["text=\"'statusValue'\""]
        assert cmd.finished


class TestProcessVisit:
    def test_enqueues_the_requested_visit(self, qaCmd, controller):
        qaCmd.process_visit(KeywordCmd(visit_id=12345))
        assert controller.processing_queue.get_nowait() == 12345

    def test_confirms_and_finishes(self, qaCmd):
        cmd = KeywordCmd(visit_id=12345)
        qaCmd.process_visit(cmd)

        assert cmd.informs == ['text="Enqueued visit_id=12345 for QA processing"']
        assert cmd.finished

    def test_accepts_an_opscore_typed_visit_id(self, qaCmd, controller):
        qaCmd.process_visit(KeywordCmd(visit_id=types.Int()("98765")))
        assert controller.processing_queue.get_nowait() == 98765

    def test_the_enqueued_visit_is_a_plain_int(self, qaCmd, controller):
        # Whichever way a visit arrives, the queue holds the same type: `Drp`
        # already casts, and an opscore Int would otherwise leak from this path
        # into `current_visit` and the logs.
        qaCmd.process_visit(KeywordCmd(visit_id=types.Int()("98765")))
        assert type(controller.processing_queue.get_nowait()) is int

    def test_a_plain_int_visit_id_is_unaffected(self, qaCmd, controller):
        qaCmd.process_visit(KeywordCmd(visit_id=12345))
        visitId = controller.processing_queue.get_nowait()
        assert visitId == 12345
        assert type(visitId) is int

    def test_logs_the_manual_enqueue(self, qaCmd, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            qaCmd.process_visit(KeywordCmd(visit_id=12345))

        assert "Manually enqueuing visit_id=12345 for QA processing" in caplog.text
