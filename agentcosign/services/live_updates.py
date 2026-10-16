"""Live update hints for the merchant's phone.

The stream only says that something changed. It never carries amounts, names or
states: the phone re-reads the server, which stays the one source of truth.
"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable

CHECK_SECONDS = 2
# quiet checks between keep-alives, so idle connections are not closed by proxies
KEEPALIVE_CHECKS = 7


def _changed(version: int) -> str:
    body = json.dumps({"version": version}, separators=(",", ":"))
    return f"event: changed\ndata: {body}\n\n"


async def change_events(
    version: Callable[[], Awaitable[int]],
    session_ok: Callable[[], Awaitable[bool]],
    wait: Callable[[int], Awaitable[None]],
    *,
    check_seconds: int = CHECK_SECONDS,
    keepalive_every: int = KEEPALIVE_CHECKS,
) -> AsyncIterator[str]:
    """Server-sent events: one hint per change, coalesced between checks.

    The first event gives the current version so the phone can sync at once.
    The stream ends with a signed_out event when the session expires. Both
    checks are awaited, so database reads run off the event loop.
    """
    last = await version()
    yield _changed(last)
    quiet = 0
    while True:
        await wait(check_seconds)
        if not await session_ok():
            yield "event: signed_out\ndata: {}\n\n"
            return
        current = await version()
        if current != last:
            last = current
            quiet = 0
            yield _changed(current)
            continue
        quiet += 1
        if quiet >= keepalive_every:
            quiet = 0
            yield ": keep-alive\n\n"
