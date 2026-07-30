import queue
from typing import override

from actorcore.Actor import Actor


class Drp:
    def __init__(self, *, actor: Actor, processing_queue: queue.Queue, logger):
        self.actor = actor
        self.logger = logger
        self.queue = processing_queue

    @override
    def receiveStatusKeys(self, key):
        self.logger.info(
            f"receiveStatusKeys: "
            f"{key.actor},"
            f"{key.name},"
            f"{key.timestamp},"
            f"{key.isCurrent},"
            f"{key.isGenuine},"
            f"{[x.__class__.baseType(x) if x is not None else None for x in key.valueList]}"
        )

        if key.name == "reduceExposureStatus" and key.isCurrent and key.isGenuine:
            # Get visit_ids from message and add to processing queue.
            if not key.valueList:
                self.logger.warning("receiveStatusKeys: empty valueList, ignoring")
                return

            visit_id = int(key.valueList[0])
            self.logger.info(f"Adding {visit_id} to QA processing queue")
            self.queue.put(visit_id)
