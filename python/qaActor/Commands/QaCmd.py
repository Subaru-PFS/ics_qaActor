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
            ("start", "", self.start_worker),
            ("stop", "", self.stop_worker),
            ("restart", "", self.restart_worker),
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
        # Show the number of items in the queue.
        q_size = self._get_controller().queue_size()

        cmd.inform(f'text="QA processing queue size: {q_size}"')
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

    def start_worker(self, cmd):
        """Start the QA worker thread."""
        self._get_controller().start(cmd=cmd)

    def stop_worker(self, cmd):
        """Stop the QA worker thread."""
        self._get_controller().stop(cmd=cmd)

    def restart_worker(self, cmd):
        """Restart the QA worker thread."""
        self._get_controller().restart(cmd=cmd)

    def process_visit(self, cmd):
        """Manually enqueue a visit_id for QA processing."""
        visit_id = cmd.cmd.keywords["visit_id"].values[0]
        self.actor.logger.info(f"Manually enqueuing {visit_id=} for QA processing")
        self._get_controller().enqueue_visit(visit_id)
        cmd.inform(f'text="Enqueued {visit_id=} for QA processing"')
        cmd.finish()
