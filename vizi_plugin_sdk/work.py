"""Long-running work: progress and cancellation (`control_plane_protocol.md`
§8.16).

Two kinds of work, one message each way:

- **A request the host is waiting on** -- a command, a reader's open or read,
  an export chunk.  The host names it by its request id; the handler reports
  through :func:`report_progress` (or ``ctx.progress``) and notices a cancel
  through :func:`cancelled` (or ``ctx.cancelled``).  The request keeps the
  session's in-flight slot until it is answered, canceled or not.
- **A background task** the plugin runs alongside, blocking nothing: begun with
  ``ctx.start_task(title)``, reported on the :class:`TaskHandle`, ended with
  :meth:`TaskHandle.done`.

Progress is a coalescing push (§5.4): report as often as is convenient and the
host shows the latest.  Canceling is cooperative -- nothing is interrupted; a
handler that notices stops, and raising :class:`Cancelled` answers the request
``Error{CANCELLED}``.
"""

from __future__ import annotations

import itertools
import math
import threading
from collections.abc import Callable

from ._lazy import LazyProto as _LazyProto

pb2 = _LazyProto("pb2")

#: §9: open background tasks per session.  A plugin needing more is reporting
#: at a grain no person can follow; the host shows at most this many rows.
MAX_OPEN_TASKS = 64

#: §9: a task's title and any work's text, in UTF-8 bytes.  Longer is cut at a
#: character boundary by the sender, and by the host if it arrives anyway: it
#: is one line of a status list, never data.
MAX_WORK_TEXT_BYTES = 256


class Cancelled(BaseException):
    """Raise from a handler that stopped because the host canceled its work;
    the request is answered ``Error{CANCELLED}``, which is not a fault.

    A ``BaseException``, as ``asyncio.CancelledError`` is, so that the
    ``except Exception`` a handler (or the SDK around it) uses for its own
    failures cannot turn a cancel into one."""


def clip_text(text: str) -> str:
    """``text`` cut to :data:`MAX_WORK_TEXT_BYTES` at a character boundary."""
    raw = str(text).encode("utf-8")
    if len(raw) <= MAX_WORK_TEXT_BYTES:
        return str(text)
    return raw[:MAX_WORK_TEXT_BYTES].decode("utf-8", errors="ignore")


def _progress(fraction: "float | None"):
    if fraction is None:
        return pb2.Progress(indeterminate=True)
    value = float(fraction)
    return pb2.Progress(value=min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0)


class _Work:
    """One request's or task's cancel flag and its way to report."""

    def __init__(self, emit: Callable, subject: str, ident: str) -> None:
        self._emit = emit
        self._subject = subject  # "request_id" | "task_id"
        self.id = ident
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def _cancel(self) -> None:
        self._cancelled.set()

    def _send(self, **fields) -> None:
        msg = pb2.WorkProgress(**{self._subject: self.id}, **fields)
        self._emit(pb2.PluginMsg(work_progress=msg))

    def progress(self, fraction: "float | None" = None, text: str = "") -> None:
        """Report how far along this is: ``fraction`` in [0, 1], or ``None``
        when there is no measure (the host shows activity), and one line of
        ``text``."""
        self._send(progress=_progress(fraction), text=clip_text(text))


class TaskHandle(_Work):
    """Background work the plugin runs alongside, blocking no request (§8.16).

    Shown to the person until :meth:`done`; with ``cancellable`` they can ask
    it to stop, which sets :attr:`cancelled`.  A context manager: leaving the
    ``with`` block ends the task, with the exception's text if one escaped."""

    def __init__(self, registry: "WorkRegistry", ident: str, title: str, cancellable: bool):
        super().__init__(registry._emit, "task_id", ident)
        self._registry = registry
        self.title = clip_text(title)
        self._cancellable = bool(cancellable)
        self._ended = False

    def progress(self, fraction: "float | None" = None, text: str = "") -> None:
        if self._ended:
            return
        self._send(
            title=self.title,
            progress=_progress(fraction),
            text=clip_text(text),
            cancellable=self._cancellable,
        )

    def done(self, message: str = "") -> None:
        """End the task; ``message`` is its outcome, shown to the person."""
        if self._ended:
            return
        self._ended = True
        self._registry._forget_task(self.id)
        self._send(title=self.title, text=clip_text(message), done=True)

    def __enter__(self) -> "TaskHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is None:
            self.done()
        elif isinstance(exc, Cancelled):
            self.done("canceled")
        else:
            self.done(f"failed: {exc}")


class WorkRegistry:
    """A session's work in progress: requests by the host's id, tasks by ours.

    A request is registered when it is DISPATCHED, on the loop, so a cancel
    that arrives before its handler starts is not lost; the handler then finds
    it already canceled and is never run."""

    def __init__(self, emit: Callable) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._requests: dict[str, _Work] = {}
        self._tasks: dict[str, TaskHandle] = {}
        self._task_ids = itertools.count(1)

    def begin_request(self, request_id: str) -> _Work:
        work = _Work(self._emit, "request_id", request_id)
        with self._lock:
            self._requests[request_id] = work
        return work

    def request(self, request_id: str) -> "_Work | None":
        with self._lock:
            return self._requests.get(request_id)

    def end_request(self, request_id: str) -> None:
        with self._lock:
            self._requests.pop(request_id, None)

    def start_task(self, title: str, cancellable: bool = True) -> TaskHandle:
        with self._lock:
            if len(self._tasks) >= MAX_OPEN_TASKS:
                raise RuntimeError(
                    f"{MAX_OPEN_TASKS} background tasks are already open "
                    "(control_plane_protocol.md §9); end one first"
                )
            task = TaskHandle(self, f"t{next(self._task_ids)}", title, cancellable)
            self._tasks[task.id] = task
        task.progress(None)
        return task

    def _forget_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def cancel(self, cancel_work) -> bool:
        """Apply a ``CancelWork``; False when it names nothing open, which is
        a no-op (the work finished as the host asked).  A task begun
        ``cancellable=False`` is flagged all the same: the flag is advice to
        the host's UI, and a task that cannot stop just goes on (§8.16)."""
        subject = cancel_work.WhichOneof("subject")
        ident = getattr(cancel_work, subject) if subject else ""
        with self._lock:
            if subject == "request_id":
                work = self._requests.get(ident)
            elif subject == "task_id":
                work = self._tasks.get(ident)
            else:
                work = None
        if work is None:
            return False
        work._cancel()
        return True

    def open_tasks(self) -> "tuple[TaskHandle, ...]":
        with self._lock:
            return tuple(self._tasks.values())


_current = threading.local()


def _set_current(work: "_Work | None") -> None:
    _current.work = work


def current_work() -> "_Work | None":
    """The request this thread's handler is serving, or ``None``."""
    return getattr(_current, "work", None)


def report_progress(fraction: "float | None" = None, text: str = "") -> None:
    """Report progress on the request this handler is serving -- a command, a
    reader's open or read, an export chunk.  Outside a handler it does
    nothing, so a helper shared with other code may call it freely."""
    work = current_work()
    if work is not None:
        work.progress(fraction, text)


def cancelled() -> bool:
    """Whether the host has asked the request this handler serves to stop.
    Check it between steps; raise :class:`Cancelled` (or return) to stop."""
    work = current_work()
    return work is not None and work.cancelled
