"""Tests for the Drp model (qaActor/models/drp.py).

This is the MHS callback that turns a `reduceExposureStatus` key from drpActor
into a visit on the QA queue. The keyvar payloads are built from the real
`opscore.protocols.types`, so the value objects behave exactly as they do live.
"""

import logging

import opscore.protocols.types as types
import pytest

from qaActor.models.drp import Drp

from .conftest import FakeKey


def intValue(n):
    """Build an opscore Int, the way a real keyvar carries one."""
    return types.Int()(str(n))


@pytest.fixture
def drp(actor, processingQueue, logger):
    return Drp(actor=actor, processing_queue=processingQueue, logger=logger)


class TestConstruction:
    def test_takes_its_collaborators_by_keyword(self, actor, processingQueue, logger):
        model = Drp(actor=actor, processing_queue=processingQueue, logger=logger)
        assert model.actor is actor
        assert model.queue is processingQueue
        assert model.logger is logger

    def test_rejects_positional_arguments(self, actor, processingQueue, logger):
        with pytest.raises(TypeError):
            Drp(actor, processingQueue, logger)


class TestReceiveStatusKeys:
    def test_enqueues_the_visit_from_a_good_key(self, drp, processingQueue):
        drp.check_reduced_exposure_status(FakeKey(valueList=[intValue(12345)]))
        assert processingQueue.get_nowait() == 12345

    def test_the_enqueued_visit_is_a_plain_int(self, drp, processingQueue):
        drp.check_reduced_exposure_status(FakeKey(valueList=[intValue(12345)]))
        visitId = processingQueue.get_nowait()
        assert type(visitId) is int

    def test_only_the_first_value_is_used_as_the_visit(self, drp, processingQueue):
        drp.check_reduced_exposure_status(FakeKey(valueList=[intValue(111), intValue(222)]))
        assert processingQueue.get_nowait() == 111
        assert processingQueue.empty()

    def test_trailing_none_values_are_tolerated(self, drp, processingQueue):
        drp.check_reduced_exposure_status(FakeKey(valueList=[intValue(777), None]))
        assert processingQueue.get_nowait() == 777

    @pytest.mark.parametrize(
        ("kwargs", "reason"),
        [
            ({"name": "someOtherKey"}, "a different key"),
            ({"isCurrent": False}, "a stale key"),
            ({"isGenuine": False}, "a non-genuine key"),
            ({"isCurrent": False, "isGenuine": False}, "a stale non-genuine key"),
        ],
    )
    def test_ignores_keys_that_do_not_qualify(self, drp, processingQueue, kwargs, reason):
        drp.check_reduced_exposure_status(FakeKey(valueList=[intValue(12345)], **kwargs))
        assert processingQueue.empty(), f"{reason} must not be enqueued"

    def test_an_empty_value_list_warns_and_enqueues_nothing(self, drp, processingQueue, caplog):
        with caplog.at_level(logging.INFO):
            drp.check_reduced_exposure_status(FakeKey(valueList=[]))

        assert processingQueue.empty()
        assert "empty valueList, ignoring" in caplog.text
        assert [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_logs_the_incoming_key_before_filtering(self, drp, caplog):
        with caplog.at_level(logging.INFO):
            drp.check_reduced_exposure_status(FakeKey(name="someOtherKey", valueList=[intValue(1)]))

        assert "receiveStatusKeys: drp,someOtherKey" in caplog.text

    def test_logs_what_it_enqueued(self, drp, caplog):
        with caplog.at_level(logging.INFO):
            drp.check_reduced_exposure_status(FakeKey(valueList=[intValue(12345)]))

        assert "Adding 12345 to QA processing queue" in caplog.text

    def test_successive_keys_queue_up_in_order(self, drp, processingQueue):
        for visitId in (1, 2, 3):
            drp.check_reduced_exposure_status(FakeKey(valueList=[intValue(visitId)]))

        assert [processingQueue.get_nowait() for _ in range(3)] == [1, 2, 3]


class TestPayloadFragility:
    """Payload shapes the callback does not currently survive.

    These pin real behaviour rather than endorse it — see the notes in the
    review that accompanied this suite.
    """

    def test_a_leading_none_value_raises(self, drp):
        # int(None) — a null first value from drpActor would propagate out of
        # the callback rather than being reported as a warning.
        with pytest.raises(TypeError):
            drp.check_reduced_exposure_status(FakeKey(valueList=[None]))

    def test_an_untyped_value_raises_in_the_debug_log(self, drp):
        # The log line calls x.__class__.baseType(x); a plain int has no
        # baseType, so anything that is not an opscore type blows up on logging
        # alone, before any of the filtering runs.
        with pytest.raises(AttributeError):
            drp.check_reduced_exposure_status(FakeKey(valueList=[12345]))
