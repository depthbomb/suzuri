from __future__ import annotations
from collections.abc import Callable, Generator, AsyncGenerator
from asyncio import Task, shield, to_thread, create_task, CancelledError

class _StreamWorker:
    def __init__(self, factory: Callable[[], Generator[str]]) -> None:
        self._factory = factory
        self._stream: Generator[str] | None = None

    def advance(self) -> str | None:
        if self._stream is None:
            self._stream = self._factory()

        return next(self._stream, None)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()

async def _settle[T](task: Task[T]) -> bool:
    """Wait out cancellation without abandoning an operation still in a thread."""
    cancelled = False
    while not task.done():
        try:
            await shield(task)
        except CancelledError:
            cancelled = True
        except BaseException:
            break

    return cancelled

async def _async_stream(factory: Callable[[], Generator[str]]) -> AsyncGenerator[str]:
    worker = _StreamWorker(factory)
    pending: Task[str | None] | None = None
    try:
        while True:
            pending = create_task(to_thread(worker.advance))
            chunk = await shield(pending)
            if chunk is None:
                break

            yield chunk
    finally:
        cancelled = False
        if pending is not None:
            cancelled = await _settle(pending)
            if not pending.cancelled():
                # Retrieve failures even if the consumer was cancelled mid-chunk.
                pending.exception()

        closing = create_task(to_thread(worker.close))
        cancelled = await _settle(closing) or cancelled
        closing.result()
        if cancelled:
            raise CancelledError
