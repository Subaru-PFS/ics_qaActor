#!/usr/bin/env python

import opscore.protocols.keys as keys
import opscore.protocols.types as types


class QaCmd:
    def __init__(self, actor):
        self.actor = actor
        self.vocab = [
            ("ping", "", self.ping),
            ("status", "", self.status),
            ("show", "", self.show),
            ("process", "<visit_id>", self.process_visit),
        ]
        self.keys = keys.KeysDictionary(
            "qa_qa",
            (1, 1),
            keys.Key("visit_id", types.Int(), help="Visit ID to enqueue for QA processing"),
        )
        self.config = actor.actorConfig

    def _get_controller(self):
        return self.actor.controllers["qa"]

    def ping(self, cmd):
        """Return a product name."""
        cmd.inform(f'text="{self.actor.productName}"')
        cmd.finish()

    def status(self, cmd):
        """Return status keywords."""
        controller = self._get_controller()

        # Show the visit being worked on, then the number of items still queued.
        visit_id = controller.current_visit
        if visit_id is None:
            cmd.inform('text="QA currently processing: idle"')
        else:
            cmd.inform(f'text="QA currently processing: {visit_id}"')

        cmd.inform(f'text="QA processing queue size: {controller.queue_size()}"')
        cmd.finish()

    def show(self, cmd):
        """Show status keywords from all models."""
        for n in self.actor.models:
            try:
                d = self.actor.models[n].keyVarDict
                for _k, v in d.items():
                    cmd.inform(f'text="{v!r}"')
            except Exception as e:
                cmd.warn(f'text="QaCmd.show: {n}: {e}"')
        cmd.finish()

    def process_visit(self, cmd):
        """Manually enqueue a visit_id for QA processing."""
        visit_id = cmd.cmd.keywords["visit_id"].values[0]
        self.actor.logger.info(f"Manually enqueuing {visit_id=} for QA processing")
        self._get_controller().enqueue_visit(visit_id)
        cmd.inform(f'text="Enqueued {visit_id=} for QA processing"')
        cmd.finish()
