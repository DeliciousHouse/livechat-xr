"""Join real session writers before disposable relay data or patches go away."""
from contextlib import contextmanager
import threading
from unittest import mock


@contextmanager
def session_writes():
    original = threading.Thread
    writers = []

    def thread(*args, **kwargs):
        worker = original(*args, **kwargs)
        if getattr(kwargs.get("target"), "__name__", "") == "save_session":
            writers.append(worker)
        return worker

    with mock.patch.object(threading, "Thread", side_effect=thread):
        try:
            yield writers
        finally:
            for worker in writers:
                worker.join(2)
                assert not worker.is_alive(), "session persistence did not finish"
