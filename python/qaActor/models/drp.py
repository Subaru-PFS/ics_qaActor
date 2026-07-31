import queue

from actorcore.Actor import Actor


class Drp:
    def __init__(self, *, actor: Actor, processing_queue: queue.Queue, logger):
        self.actor = actor
        self.logger = logger
        self.queue = processing_queue

    def check_reduced_exposure_status(self, key):
        """Check the reduced exposure status key and add visit_ids to the processing queue.

        This callback is triggered when the DRP actor emits a reduceExposureStatus
        key. If the key is current and genuine, the visit_id from the key's value
        list is extracted and added to the QA processing queue.

        Parameters
        ----------
        key : actorkeys.Key
            The key object from the DRP actor containing reduceExposureStatus information.
            Expected to have attributes: name, actor, timestamp, isCurrent, isGenuine,
            and valueList where valueList[0] contains the visit_id.

        Notes
        -----
        - Only processes keys that are both current and genuine
        - Logs a warning if the valueList is empty
        - The visit_id is cast to int before being added to the queue
        """
        self.logger.info(
            f"check_reduced_exposure_status: "
            f"{key.actor},"
            f"{key.name},"
            f"{key.timestamp},"
            f"{key.isCurrent},"
            f"{key.isGenuine},"
            f"{[x.__class__.baseType(x) if x is not None else None for x in key.valueList]}"
        )

        if key.isCurrent and key.isGenuine:
            # Get visit_ids from message and add to processing queue.
            if not key.valueList:
                self.logger.warning("check_reduced_exposure_status: empty valueList, ignoring")
                return

            visit_id = int(key.valueList[0])
            self.logger.info(f"Adding {visit_id} to QA processing queue")
            self.queue.put(visit_id)
