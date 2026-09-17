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

    def _reduction_succeeded(self, visit_id, value_list):
        """Report whether a reduceExposureStatus payload describes a good reduction.

        Parameters
        ----------
        visit_id : int
            The visit the payload describes, for the log message.
        value_list : sequence
            The key's values, `(visit, returnCode, statusStr, timing)`.

        Returns
        -------
        bool
            False only when the payload carries a non-zero returnCode. A payload
            with no returnCode at all is treated as success: that is the shape
            older drpActor builds send, and refusing to process it would drop
            work that is almost certainly fine.
        """
        if len(value_list) < 2 or value_list[1] is None:
            return True

        return_code = int(value_list[1])
        if return_code == 0:
            return True

        status = value_list[2] if len(value_list) > 2 and value_list[2] is not None else ""
        self.logger.warning(
            f"reduceExposure failed for {visit_id} "
            f"(returnCode {return_code}{f': {status}' if status else ''}), skipping QA"
        )
        return False

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
        - Skips visits whose reduction reported a non-zero returnCode
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

            # valueList is (visit, returnCode, statusStr, timing). A non-zero
            # returnCode means the reduction itself failed, so there is nothing
            # worth running QA over. drpActor only ever sends 0 today, which is
            # exactly why this is worth pinning: the day it reports a failure,
            # the default must not be to reduce-then-QA regardless.
            if not self._reduction_succeeded(visit_id, key.valueList):
                return

            try:
                processing_queue = self.queue
            except KeyError:
                self.logger.warning(
                    f"check_reduced_exposure_status: QA controller is not attached, dropping {visit_id}"
                )
                return

            self.logger.info(f"Adding {visit_id} to QA processing queue")
            processing_queue.put(visit_id)
