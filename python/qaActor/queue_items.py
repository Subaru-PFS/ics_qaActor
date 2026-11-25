"""Items placed on the QA controller's processing queue.

These deliberately live outside the ``qa`` controller module. ``ICC.attachController``
reloads the controller module (``importlib.reload``) on every attach, and ``connectionMade``
re-attaches the controller on every hub reconnect, so each dataclass *defined* in that module
becomes a fresh class object after a reconnect. The ``Drp`` model, by contrast, is built once and
reused across reconnects (see the actor's ``connectionMade``): it captures the class it enqueues at
import time. If the queue items lived in the reloaded module, a ``MarkFailed`` the model created would
fail the controller's ``isinstance`` check against the post-reload class and be mishandled as an
ordinary visit. Keeping them here — in a module ``attachController`` never reloads — keeps a single
class identity shared by the long-lived producer and the reloaded consumer.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LoadVisit:
    """A queue item asking for a visit's load alone, without its QA: `qa twilight <visit>`."""

    visit: int


@dataclass(frozen=True)
class DefineChain:
    """A queue item asking for the output chain to be defined (or repaired): put first at start."""


@dataclass(frozen=True)
class MarkFailed:
    """A queue item asking that a visit's reduceExposure failure be recorded in qadb (REDUCE FAILED).

    The `Drp` model enqueues it when ``reduceExposureStatus`` reports a non-zero returnCode, so QA is
    skipped but the dashboard still shows the failure rather than leaving the visit awaiting QA.
    """

    visit: int
    return_code: int | None = None
    status: str = ""
