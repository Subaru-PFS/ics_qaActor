#!/usr/bin/env python

import opscore.protocols.keys as keys
import opscore.protocols.types as types
from opscore.utility.qstr import qstr


class QaCmd:
    def __init__(self, actor):
        self.actor = actor
        self.vocab = [
            ("ping", "", self.ping),
            ("status", "", self.status),
            ("show", "", self.show),
            ("process", "<visit_id>", self.process_visit),
            ("qadb", "@(on|off)", self.qadb),
            ("twilight", "<visit_id>", self.twilight),
        ]
        self.keys = keys.KeysDictionary(
            "qa_qa",
            (1, 1),
            keys.Key("visit_id", types.Int(), help="Visit ID to enqueue for QA processing"),
        )

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
        cmd.inform(f"text={qstr(controller.describe_qadb_load())}")
        cmd.inform(f"text={qstr(controller.describe_twilight())}")
        cmd.finish()

    def show(self, cmd):
        """Show status keywords from all models.

        Values go out through `qstr`: a keyvar repr embeds the values' own reprs,
        and Python switches a string's repr to double quotes as soon as it holds
        an apostrophe — `String("can't open file")`. Interpolated raw, that inner
        quote closes the `text="..."` value early and corrupts the MHS line.
        """
        for n in self.actor.models:
            try:
                d = self.actor.models[n].keyVarDict
                for _k, v in d.items():
                    cmd.inform(f"text={qstr(repr(v))}")
            except Exception as e:
                cmd.warn(f"text={qstr(f'QaCmd.show: {n}: {e}')}")
        cmd.finish()

    def process_visit(self, cmd):
        """Manually enqueue a visit_id for QA processing.

        The value is cast to a plain int so the queue holds one type whichever
        way a visit arrives. `Drp` already casts; opscore hands this path an
        `Int`, which subclasses int and so works untouched, but leaks an opscore
        type into `current_visit` and into the logs.
        """
        visit_id = int(cmd.cmd.keywords["visit_id"].values[0])
        self.actor.logger.info(f"Manually enqueuing {visit_id=} for QA processing")
        self._get_controller().enqueue_visit(visit_id)
        cmd.inform(f'text="Enqueued {visit_id=} for QA processing"')
        cmd.finish()

    def qadb(self, cmd):
        """Switch loading each visit into qadb on or off, until the controller is rebuilt.

        It applies from the next visit's load, including the visit in `pipetask` now; the
        config's `engine.qadb.load` applies again after an actor restart or controller reconnect.
        """
        enabled = "on" in cmd.cmd.keywords
        try:
            self._get_controller().set_qadb_load(enabled)
        except RuntimeError as e:
            cmd.fail(f"text={qstr(str(e))}")
            return
        message = self._get_controller().describe_qadb_load()
        self.actor.logger.info(message)
        cmd.finish(f"text={qstr(message)}")

    def twilight(self, cmd):
        """Load a visit into qadb again, without re-running its QA; the load judges its twilight sequence.

        The command keeps its name from when a separate judge ran: drp_qa's loader now judges a
        twilight sequence itself, when a load finds it complete. This is for a sequence the automatic
        load of its last visit missed: the actor was down for that visit, its reduction was never
        reported, its status row came late, or the load failed. The loader still checks that the
        sequence is a finished twilight with every visit's tables stored. It loads any visit, twilight
        or not, and needs both databases: db.opdb to find the sequence, and qadb loading on.
        """
        visit_id = int(cmd.cmd.keywords["visit_id"].values[0])
        controller = self._get_controller()
        if controller.opdb_host is None:
            message = "cannot judge twilight sequences: the config has no db.opdb (actors.yaml)"
            cmd.fail(f"text={qstr(message)}")
            return
        if controller.load_cmd(visit_id) is None:
            message = f"cannot load {visit_id=}: {controller.describe_qadb_load()}"
            cmd.fail(f"text={qstr(message)}")
            return
        self.actor.logger.info(f"Manually enqueuing the load of {visit_id=} (qa twilight)")
        controller.enqueue_load(visit_id)
        cmd.finish(f'text="Enqueued the load of {visit_id=}, which judges its twilight sequence if complete"')
