"""The queue-item classes keep a single identity across a controller-module reload.

`ICC.attachController` reloads the `qa` controller module (`importlib.reload`) on every attach, and
the actor re-attaches the controller on every hub reconnect. The `Drp` model, however, is built once
and reused across reconnects, so it captures the queue-item class it enqueues at import time. If that
class were redefined by the reload, the controller's `isinstance` checks would reject the model's items
and a reduceExposure failure would be mishandled as an ordinary visit (its `MarkFailed` marker fed to
`pipetask`/`load` as the visit id). These tests pin that the queue items survive a reload unchanged.
"""

import importlib

import qaActor.Controllers.qa as qa_mod
import qaActor.queue_items as queue_items


class TestQueueItemsSurviveControllerReload:
    def test_markfailed_identity_is_stable_across_reload(self):
        # The class the producer (the long-lived Drp model) holds, captured before any reload.
        producer_cls = qa_mod.MarkFailed
        assert producer_cls is queue_items.MarkFailed

        # Reload the controller module exactly as attachController does on each attach.
        importlib.reload(qa_mod)

        # The consumer's class after the reload is the very object the producer still holds, so
        # `isinstance(item, MarkFailed)` in the run loop keeps matching a model-made item.
        assert qa_mod.MarkFailed is producer_cls
        assert isinstance(producer_cls(156945, 1, "FAILED"), qa_mod.MarkFailed)

    def test_reload_really_does_mint_fresh_module_classes(self):
        # Negative control: reloading does re-create classes defined *in* the controller module,
        # which is exactly why a queue item must not live there. The `qa` controller class, defined
        # in qa.py, is a new object after the reload; the imported queue item is not.
        controller_before = qa_mod.qa
        markfailed_before = qa_mod.MarkFailed

        importlib.reload(qa_mod)

        assert qa_mod.qa is not controller_before  # redefined by the reload
        assert qa_mod.MarkFailed is markfailed_before  # imported from the stable module, untouched

    def test_every_queue_item_comes_from_the_stable_module(self):
        for name in ("MarkFailed", "LoadVisit", "DefineChain"):
            assert getattr(qa_mod, name) is getattr(queue_items, name)
