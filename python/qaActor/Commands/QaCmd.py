#!/usr/bin/env python

import opscore.protocols.keys as keys


class QaCmd:
    def __init__(self, actor):

        self.actor = actor
        self.vocab = [
            ("ping", "", self.ping),
            ("status", "", self.status),
            ("show", "", self.show),
        ]
        self.keys = keys.KeysDictionary(
            "qa_qa",
            (1, 1),
        )

    def ping(self, cmd):
        """Return a product name."""

        cmd.inform(f'text="{self.actor.productName}"')
        cmd.finish()

    def status(self, cmd):
        """Return status keywords."""

        self.actor.sendVersionKey(cmd)

        # Show the number of items in the queue.
        q_size = self.actor.processing_queue.qsize()
        self.actor.logger.info(f"QA Processing queue size: {q_size}")
        cmd.inform(f'text="QA processing queue size: {q_size}"')
        cmd.finish()

    def show(self, cmd):
        """Show status keywords from all models."""

        for n in self.actor.models:
            try:
                d = self.actor.models[n].keyVarDict
                for k, v in d.items():
                    cmd.inform(f'text="{repr(v)}"')
            except Exception as e:
                cmd.warn(f'text="QaCmd.show: {n}: {e}"')
        cmd.finish()
