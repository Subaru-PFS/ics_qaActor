import queue

from actorcore.Actor import Actor


class Drp:
    def __init__(self, *, actor: Actor, logger):
        self.actor = actor
        self.logger = logger

    @property
    def queue(self) -> queue.Queue:
        """The live QA controller's processing queue.

        Resolved on every access rather than captured at construction: the actor
        re-runs `connectionMade` on each hub reconnect, and `ICC.attachController`
        builds a fresh `qa` controller — with a fresh queue — each time. A queue
        captured once would be orphaned by the first reconnect, and every visit
        put on it afterwards would be dropped without a trace.

        Raises
        ------
        KeyError
            If the QA controller is not currently attached.
        """
        return self.actor.controllers["qa"].processing_queue

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
            and valueList where valueList[0] contains the visit_id. See
            `drpActor.utils.engine.processVisitGroup`

        Notes
        -----
        - Only processes keys that are both current and genuine
        - Logs a warning if the valueList is empty
        - The visit_id is cast to int before being added to the queue
        - Logs a warning, rather than raising, if the QA controller is not attached;
          opscore swallows exceptions raised out of keyvar callbacks
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

            try:
                processing_queue = self.queue
            except KeyError:
                self.logger.warning(
                    f"check_reduced_exposure_status: QA controller is not attached, dropping {visit_id}"
                )
                return

            self.logger.info(f"Adding {visit_id} to QA processing queue")
            processing_queue.put(visit_id)
