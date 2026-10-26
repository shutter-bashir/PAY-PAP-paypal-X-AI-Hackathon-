"""The status poller."""
import threading

from app import worker


class FakePayPal:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_run_polls_until_stopped(monkeypatch):
    ticked = threading.Event()
    monkeypatch.setattr(worker.Store, "from_settings", classmethod(lambda cls, s: object()))
    monkeypatch.setattr(worker.PayPalClient, "from_settings", classmethod(lambda cls, s: FakePayPal()))
    monkeypatch.setattr(worker, "poll_once", lambda store, paypal: ticked.set())
    monkeypatch.setattr(worker, "expire_approvals", lambda store: 0)
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,))
    thread.start()
    assert ticked.wait(5)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
