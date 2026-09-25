"""Resource Manager - Single owner pattern for all hardware connections.

The Resource Manager is the core of ScopeLoop's control plane. It:
- Owns all connections to instruments and devices
- Serializes access through locking/arbitration
- Ensures MCP server and UI are clients, not owners

This prevents race conditions when multiple clients (agent, UI) try to
access the same hardware simultaneously.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TaskRLock:
    """Serialize complete workflows; nesting is allowed only in the owning task."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if self._owner is not task:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(self, *args):
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()


def serialized(method):
    """Use the same task lock for a driver operation and its nested transactions."""

    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with self._lock:
            return await method(self, *args, **kwargs)

    return wrapper


class HostLease:
    """Fail-fast OS lease shared by ScopeLoop processes, released on process exit.

    All clients of a shared host must use the same SCOPELOOP_LOCK_DIR. This does
    not arbitrate raw third-party sockets or front-panel operations.
    """

    def __init__(self, key: str, directory: Path | None = None):
        self.key = key
        root = directory or Path(os.environ.get("SCOPELOOP_LOCK_DIR", "~/.scopeloop/locks"))
        self.path = root.expanduser() / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
        self._file = None

    def acquire(self):
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b" ")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ResourceError(f"Resource busy: {self.key}; lease {self.path}") from exc
        self._file = handle
        handle.seek(1)
        handle.truncate()
        handle.write(
            json.dumps(
                {"pid": os.getpid(), "resource": self.key, "acquired_at": time.time()}
            ).encode()
        )
        handle.flush()

    def release(self):
        if self._file is not None:
            self._file.close()
            self._file = None


class ResourceState(Enum):
    """State of a managed resource."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class LockMode(Enum):
    """Lock acquisition mode."""

    EXCLUSIVE = "exclusive"  # Full read/write access
    OBSERVE = "observe"  # Instrument reads also serialize; use cached state for observers
    CONTROL_TRANSFER = "control_transfer"  # Wait for explicit release; never preempt


class ClientPriority(Enum):
    """Priority level for lock arbitration."""

    AGENT = 100  # LLM agent gets highest priority during autonomous runs
    UI = 50  # UI gets medium priority
    SYSTEM = 200  # System operations (safety, etc.) get absolute priority


@dataclass
class LockInfo:
    """Information about a held lock."""

    resource_id: str
    client_id: str
    mode: LockMode
    priority: ClientPriority
    acquired_at: float
    timeout: float | None = None
    task: Any = None
    depth: int = 1


@dataclass
class ResourceInfo:
    """Information about a managed resource."""

    resource_id: str
    resource_type: str  # e.g., "oscilloscope", "serial", "logic_analyzer"
    state: ResourceState = ResourceState.DISCONNECTED
    connection: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None


class ResourceLock:
    """Async context manager for resource locks."""

    def __init__(
        self,
        manager: ResourceManager,
        resource_id: str,
        client_id: str,
        mode: LockMode,
        priority: ClientPriority,
        timeout: float | None = None,
    ):
        self._manager = manager
        self._resource_id = resource_id
        self._client_id = client_id
        self._mode = mode
        self._priority = priority
        self._timeout = timeout
        self._acquired = False

    async def __aenter__(self) -> ResourceLock:
        await self._manager._acquire_lock(
            self._resource_id,
            self._client_id,
            self._mode,
            self._priority,
            self._timeout,
        )
        self._acquired = True
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._acquired:
            await self._manager._release_lock(self._resource_id, self._client_id)
            self._acquired = False

    @property
    def resource_id(self) -> str:
        return self._resource_id

    @property
    def client_id(self) -> str:
        return self._client_id


class ResourceError(Exception):
    """Error related to resource management."""

    pass


class LockTimeoutError(ResourceError):
    """Lock acquisition timed out."""

    pass


class ResourceNotFoundError(ResourceError):
    """Resource not found."""

    pass


class ResourceManager:
    """Single owner of all hardware connections.

    The ResourceManager owns all instrument connections and serializes
    access from multiple clients (MCP server, UI, etc.).

    Usage:
        manager = ResourceManager()

        # Register a resource
        await manager.register("scope", "oscilloscope", connect_func, disconnect_func)

        # Acquire lock and use resource
        async with manager.lock("scope", client_id, LockMode.EXCLUSIVE) as lock:
            result = await manager.execute("scope", client_id, some_command)

        # Or use execute directly (auto-acquires lock)
        result = await manager.execute("scope", client_id, some_command)
    """

    def __init__(self, default_lock_timeout: float = 30.0):
        self._resources: dict[str, ResourceInfo] = {}
        self._locks: dict[str, LockInfo | None] = {}
        self._lock_conditions: dict[str, asyncio.Condition] = {}
        self._connect_funcs: dict[str, Callable[[], Coroutine[Any, Any, Any]]] = {}
        self._disconnect_funcs: dict[str, Callable[[Any], Coroutine[Any, Any, None]]] = {}
        self._default_lock_timeout = default_lock_timeout
        self._event_callbacks: list[Callable[[str, str, dict[str, Any]], None]] = []

    async def register(
        self,
        resource_id: str,
        resource_type: str,
        connect_func: Callable[[], Coroutine[Any, Any, Any]],
        disconnect_func: Callable[[Any], Coroutine[Any, Any, None]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Register a resource with the manager.

        Args:
            resource_id: Unique identifier for the resource.
            resource_type: Type of resource (e.g., "oscilloscope").
            connect_func: Async function to establish connection.
            disconnect_func: Async function to close connection.
            metadata: Optional metadata about the resource.
        """
        if resource_id in self._resources:
            raise ResourceError(f"Resource already registered: {resource_id}")

        self._resources[resource_id] = ResourceInfo(
            resource_id=resource_id,
            resource_type=resource_type,
            metadata=metadata or {},
        )
        self._locks[resource_id] = None
        self._lock_conditions[resource_id] = asyncio.Condition()
        self._connect_funcs[resource_id] = connect_func
        self._disconnect_funcs[resource_id] = disconnect_func

        logger.info(f"Registered resource: {resource_id} ({resource_type})")
        self._emit_event("resource_registered", resource_id, {"type": resource_type})

    async def unregister(self, resource_id: str) -> None:
        """Unregister a resource and disconnect if connected."""
        if resource_id not in self._resources:
            raise ResourceNotFoundError(f"Unknown resource: {resource_id}")

        owner = self._locks[resource_id]
        if owner is not None and owner.task is not asyncio.current_task():
            raise ResourceError(f"Resource busy: {resource_id} ({owner.client_id})")
        resource = self._resources[resource_id]
        if resource.state == ResourceState.CONNECTED:
            await self.disconnect(resource_id)

        del self._resources[resource_id]
        del self._locks[resource_id]
        del self._lock_conditions[resource_id]
        del self._connect_funcs[resource_id]
        del self._disconnect_funcs[resource_id]

        logger.info(f"Unregistered resource: {resource_id}")
        self._emit_event("resource_unregistered", resource_id, {})

    async def connect(self, resource_id: str) -> Any:
        """Connect to a resource.

        Args:
            resource_id: The resource to connect to.

        Returns:
            The connection object.
        """
        if resource_id not in self._resources:
            raise ResourceNotFoundError(f"Unknown resource: {resource_id}")

        owner = self._locks[resource_id]
        if owner is not None and owner.task is not asyncio.current_task():
            raise ResourceError(f"Resource busy: {resource_id} ({owner.client_id})")
        resource = self._resources[resource_id]
        if resource.state == ResourceState.CONNECTED:
            return resource.connection

        if resource.state == ResourceState.CONNECTING:
            raise ResourceError(f"Resource connection already in progress: {resource_id}")
        resource.state = ResourceState.CONNECTING
        self._emit_event("resource_connecting", resource_id, {})

        try:
            connection = await self._connect_funcs[resource_id]()
            resource.connection = connection
            resource.state = ResourceState.CONNECTED
            resource.last_error = None
            logger.info(f"Connected to resource: {resource_id}")
            self._emit_event("resource_connected", resource_id, {})
            return connection
        except Exception as e:
            resource.state = ResourceState.ERROR
            resource.last_error = str(e)
            logger.error(f"Failed to connect to {resource_id}: {e}")
            self._emit_event("resource_error", resource_id, {"error": str(e)})
            raise ResourceError(f"Failed to connect to {resource_id}: {e}") from e

    async def disconnect(self, resource_id: str) -> None:
        """Disconnect from a resource."""
        if resource_id not in self._resources:
            raise ResourceNotFoundError(f"Unknown resource: {resource_id}")

        owner = self._locks[resource_id]
        if owner is not None and owner.task is not asyncio.current_task():
            raise ResourceError(f"Resource busy: {resource_id} ({owner.client_id})")
        resource = self._resources[resource_id]
        if resource.state != ResourceState.CONNECTED:
            return

        try:
            await self._disconnect_funcs[resource_id](resource.connection)
        except Exception as e:
            logger.warning(f"Error disconnecting {resource_id}: {e}")
        finally:
            resource.connection = None
            resource.state = ResourceState.DISCONNECTED
            logger.info(f"Disconnected from resource: {resource_id}")
            self._emit_event("resource_disconnected", resource_id, {})

    @asynccontextmanager
    async def lock(
        self,
        resource_id: str,
        client_id: str,
        mode: LockMode = LockMode.EXCLUSIVE,
        priority: ClientPriority = ClientPriority.UI,
        timeout: float | None = None,
    ) -> AsyncIterator[ResourceLock]:
        """Acquire a lock on a resource.

        Args:
            resource_id: The resource to lock.
            client_id: Identifier for the client requesting the lock.
            mode: Lock mode (exclusive, observe, or control_transfer).
            priority: Client priority for arbitration.
            timeout: Lock timeout in seconds. None uses default.

        Yields:
            A ResourceLock context manager.
        """
        lock = ResourceLock(self, resource_id, client_id, mode, priority, timeout)
        try:
            await lock.__aenter__()
            yield lock
        finally:
            await lock.__aexit__(None, None, None)

    async def _acquire_lock(
        self,
        resource_id: str,
        client_id: str,
        mode: LockMode,
        priority: ClientPriority,
        timeout: float | None = None,
    ) -> None:
        """Internal: Acquire a lock on a resource."""
        if resource_id not in self._resources:
            raise ResourceNotFoundError(f"Unknown resource: {resource_id}")

        timeout = self._default_lock_timeout if timeout is None else timeout
        condition = self._lock_conditions[resource_id]

        async with condition:
            start_time = time.monotonic()

            while True:
                current_lock = self._locks[resource_id]

                # No lock held - acquire it
                if current_lock is None:
                    self._locks[resource_id] = LockInfo(
                        resource_id=resource_id,
                        client_id=client_id,
                        mode=mode,
                        priority=priority,
                        acquired_at=time.time(),
                        timeout=timeout,
                        task=asyncio.current_task(),
                    )
                    logger.debug(f"Lock acquired: {resource_id} by {client_id}")
                    self._emit_event(
                        "lock_acquired",
                        resource_id,
                        {"client_id": client_id, "mode": mode.value},
                    )
                    return

                # Priority and OBSERVE never grant access to another active owner.
                if (
                    current_lock.client_id == client_id
                    and current_lock.task is asyncio.current_task()
                ):
                    current_lock.depth += 1
                    return

                # Wait for lock to be released
                elapsed = time.monotonic() - start_time
                remaining = timeout - elapsed
                if remaining <= 0:
                    raise LockTimeoutError(
                        f"Timeout acquiring lock on {resource_id} "
                        f"(held by {current_lock.client_id})"
                    )

                try:
                    await asyncio.wait_for(condition.wait(), timeout=remaining)
                except TimeoutError as exc:
                    raise LockTimeoutError(
                        f"Timeout acquiring lock on {resource_id} "
                        f"(held by {current_lock.client_id})"
                    ) from exc

    async def _release_lock(self, resource_id: str, client_id: str) -> None:
        """Internal: Release a lock on a resource."""
        if resource_id not in self._resources:
            raise ResourceNotFoundError(f"Unknown resource: {resource_id}")

        condition = self._lock_conditions[resource_id]

        async with condition:
            current_lock = self._locks[resource_id]
            if (
                current_lock is not None
                and current_lock.client_id == client_id
                and current_lock.task is asyncio.current_task()
            ):
                current_lock.depth -= 1
                if current_lock.depth:
                    return
                self._locks[resource_id] = None
                logger.debug(f"Lock released: {resource_id} by {client_id}")
                self._emit_event(
                    "lock_released",
                    resource_id,
                    {"client_id": client_id},
                )
                condition.notify_all()

    async def execute(
        self,
        resource_id: str,
        client_id: str,
        command: Callable[[Any], Coroutine[Any, Any, T]],
        mode: LockMode = LockMode.EXCLUSIVE,
        priority: ClientPriority = ClientPriority.UI,
        timeout: float | None = None,
    ) -> T:
        """Execute a command on a resource (auto-acquires lock).

        Args:
            resource_id: The resource to execute on.
            client_id: Identifier for the client.
            command: Async function that takes the connection and returns a result.
            mode: Lock mode for the operation.
            priority: Client priority for arbitration.
            timeout: Lock timeout.

        Returns:
            Result of the command.
        """
        async with self.lock(resource_id, client_id, mode, priority, timeout):
            resource = self._resources[resource_id]

            # Auto-connect if needed
            if resource.state != ResourceState.CONNECTED:
                await self.connect(resource_id)

            return await command(resource.connection)

    def get_resource_info(self, resource_id: str) -> ResourceInfo | None:
        """Get information about a resource."""
        return self._resources.get(resource_id)

    def get_all_resources(self) -> dict[str, ResourceInfo]:
        """Get all registered resources."""
        return dict(self._resources)

    def get_lock_info(self, resource_id: str) -> LockInfo | None:
        """Get current lock info for a resource."""
        return self._locks.get(resource_id)

    def is_locked(self, resource_id: str) -> bool:
        """Check if a resource is currently locked."""
        return self._locks.get(resource_id) is not None

    def on_event(
        self,
        callback: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        """Register a callback for resource events.

        Callback receives: (event_type, resource_id, data)
        """
        self._event_callbacks.append(callback)

    def _emit_event(
        self,
        event_type: str,
        resource_id: str,
        data: dict[str, Any],
    ) -> None:
        """Emit an event to all registered callbacks."""
        for callback in self._event_callbacks:
            try:
                callback(event_type, resource_id, data)
            except Exception as e:
                logger.warning(f"Event callback error: {e}")

    async def shutdown(self) -> None:
        """Disconnect all resources and shutdown the manager."""
        if any(self._locks.values()):
            raise ResourceError("Cannot shut down while resources are owned")
        logger.info("Shutting down ResourceManager...")
        for resource_id in list(self._resources.keys()):
            try:
                await self.disconnect(resource_id)
            except Exception as e:
                logger.warning(f"Error disconnecting {resource_id}: {e}")

        self._resources.clear()
        self._locks.clear()
        self._lock_conditions.clear()
        logger.info("ResourceManager shutdown complete")


def generate_client_id(prefix: str = "client") -> str:
    """Generate a unique client ID."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"
