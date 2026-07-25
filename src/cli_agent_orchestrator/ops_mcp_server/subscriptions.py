from typing import Awaitable, Callable, Dict, Optional, Tuple

import anyio


class FastMcp32SessionTasks:
    """
    Adapter that owns a resource-to-cancel-scope registry on the session object.
    Uses FastMCP 3.2's `MiddlewareServerSession._subscription_task_group` seam.
    """

    def __init__(self, session: object) -> None:
        self.session = session
        if not hasattr(session, "_ops_subscription_registry"):
            # Use setattr to dynamically attach the registry to the session object
            setattr(session, "_ops_subscription_registry", {})

    @property
    def _registry(self) -> Dict[str, Tuple[anyio.CancelScope, anyio.Event]]:
        import typing

        return typing.cast(
            Dict[str, Tuple[anyio.CancelScope, anyio.Event]],
            getattr(self.session, "_ops_subscription_registry"),
        )

    async def subscribe(self, resource_uri: str, worker: Callable[[], Awaitable[None]]) -> None:
        """
        Idempotently subscribes a worker to a resource.
        """
        if resource_uri in self._registry:
            return

        task_group: Optional[anyio.abc.TaskGroup] = getattr(
            self.session, "_subscription_task_group", None
        )
        if task_group is None:
            raise RuntimeError("FastMCP 3.2 session missing _subscription_task_group seam")

        done_event = anyio.Event()
        scope = anyio.CancelScope()

        async def _scoped_wrapper() -> None:
            with scope:
                try:
                    await worker()
                except Exception as e:
                    import logging

                    logging.getLogger(__name__).warning("Subscription worker failed", exc_info=True)
                finally:
                    # Mark as done so unsubscribers can wake up
                    done_event.set()
                    # Only remove from the registry if it hasn't been replaced
                    record = self._registry.get(resource_uri)
                    if record is not None and record[1] is done_event:
                        del self._registry[resource_uri]

        # Transactional registration
        self._registry[resource_uri] = (scope, done_event)
        try:
            task_group.start_soon(_scoped_wrapper)
        except Exception:
            # Rollback if start_soon fails (e.g., RuntimeError if inactive)
            del self._registry[resource_uri]
            raise

    async def unsubscribe(self, resource_uri: str) -> None:
        """
        Cancels the active worker for the resource and awaits its termination
        before removing it from the registry.
        """
        record = self._registry.get(resource_uri)
        if record is not None:
            scope, done_event = record
            scope.cancel()
            await done_event.wait()

    async def cancel_all(self) -> None:
        """
        Explicit finalization seam: cancels all active subscription workers and awaits their termination.
        """
        events_to_wait = []
        for scope, done_event in list(self._registry.values()):
            scope.cancel()
            events_to_wait.append(done_event)

        for event in events_to_wait:
            await event.wait()
