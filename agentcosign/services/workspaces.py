"""Private workspaces for the hosted demo.

Each visitor who sets up a device gets their own workspace: a separate database
and gateway with its own signing secret, so nobody can see, change or stop
another visitor's refunds, and a sign-in from one workspace is useless in any
other. Outside the demo there is only the owner's workspace.
"""

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

from agentcosign.core.config import DemoSettings
from agentcosign.core.exceptions import LimitReachedError
from agentcosign.core.trace import Trace

WORKSPACE_ID = re.compile(r"^ws-[0-9a-f]{32}$")
DAY_SECONDS = 24 * 3600


class Workspaces[G]:
    """The owner's workspace plus the visitors' workspaces that are still open."""

    def __init__(
        self,
        *,
        default: G,
        build: Callable[[str], G],
        demo: bool,
        limits: DemoSettings,
        # guardrail: allow float-annotation -- wall-clock seconds, not money
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.default = default
        self._build = build
        self._demo = demo
        self._limits = limits
        self._clock = clock
        self._open: OrderedDict[str, G] = OrderedDict()
        # each workspace's live step record, for the technical view
        self._traces: dict[str, Trace] = {}
        self._default_trace = Trace()
        # guardrail: allow float-annotation -- wall-clock seconds, not money
        self._setups: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def open(self, address: str) -> str:
        """Open a new private workspace for one address, within the daily allowance."""
        if not self._demo:
            raise LimitReachedError("workspaces exist only in the hosted demo")
        with self._lock:
            now = self._clock()
            recent = [t for t in self._setups.get(address, []) if now - t < DAY_SECONDS]
            if len(recent) >= self._limits.setups_per_address_per_day:
                raise LimitReachedError("too many setups from this address today")
            self._setups[address] = [*recent, now]
            workspace_id = f"ws-{secrets.token_hex(16)}"
            self._open[workspace_id] = self._build(workspace_id)
            self._traces[workspace_id] = Trace()
            while len(self._open) > self._limits.max_workspaces:
                closed, _ = self._open.popitem(last=False)  # least recently used
                self._traces.pop(closed, None)
            return workspace_id

    def get(self, workspace_id: str) -> G | None:
        """The workspace's gateway, or None when it was never opened or has closed."""
        if not WORKSPACE_ID.fullmatch(workspace_id):
            return None
        with self._lock:
            gateway = self._open.get(workspace_id)
            if gateway is not None:
                self._open.move_to_end(workspace_id)
            return gateway

    def trace(self, workspace_id: str) -> Trace | None:
        """The workspace's live step record (the owner's for an empty id)."""
        if not workspace_id:
            return self._default_trace
        with self._lock:
            return self._traces.get(workspace_id)

    def live(self) -> list[G]:
        """Every workspace background work must cover, the owner's first."""
        with self._lock:
            return [self.default, *self._open.values()]
