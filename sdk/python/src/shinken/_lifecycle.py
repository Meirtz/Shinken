"""Internal helpers for exception-safe provider lifecycle composition."""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("shinken.lifecycle")


def connect_owned_handle(provider: Any, handle: Any, **connect_kwargs: Any) -> Any:
    """Connect to a newly materialized handle, destroying it if connect fails.

    Callers use this only when they own ``handle`` (for example immediately after
    ``create``/``restore``/``fork``).  A failed handshake must not orphan the substrate,
    and a secondary teardown failure must not replace the original connection error.
    """
    try:
        return provider.connect(handle, **connect_kwargs)
    except BaseException:
        try:
            provider.destroy(handle)
        except BaseException:  # noqa: BLE001 - preserve the primary connection failure
            _log.error(
                "failed to destroy handle after connect failure (type=%s)",
                type(handle).__name__,
                exc_info=True,
            )
        raise
