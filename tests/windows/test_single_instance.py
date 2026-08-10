from __future__ import annotations

from videoscope.windows.single_instance import (
    ShutdownSignal,
    SingleInstanceLease,
    request_existing_shutdown,
    wait_for_instance_exit,
)


class FakeKernel:
    def __init__(self, *, mutex_results: list[bool] | None = None) -> None:
        self.mutex_results = list(mutex_results or [False])
        self.next_handle = 10
        self.closed: list[int] = []
        self.event_set = False
        self.event_available = True

    def _handle(self) -> int:
        self.next_handle += 1
        return self.next_handle

    def create_mutex(self, name: str) -> tuple[int, bool]:
        assert name
        exists = self.mutex_results.pop(0) if self.mutex_results else False
        return self._handle(), exists

    def create_event(self, name: str) -> int:
        assert name
        return self._handle()

    def open_event(self, name: str) -> int | None:
        assert name
        return self._handle() if self.event_available else None

    def event_is_set(self, handle: int) -> bool:
        assert handle > 0
        return self.event_set

    def set_event(self, handle: int) -> bool:
        assert handle > 0
        self.event_set = True
        return True

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def test_single_instance_rejects_second_owner_and_closes_temporary_handle() -> None:
    kernel = FakeKernel(mutex_results=[True])
    lease = SingleInstanceLease(kernel=kernel)

    assert lease.acquire() is False
    assert len(kernel.closed) == 1


def test_primary_lease_and_shutdown_event_have_explicit_lifetimes() -> None:
    kernel = FakeKernel()
    lease = SingleInstanceLease(kernel=kernel)
    signal = ShutdownSignal(kernel=kernel)

    assert lease.acquire() is True
    assert signal.requested is False
    kernel.event_set = True
    assert signal.requested is True

    signal.close()
    lease.close()
    assert len(kernel.closed) == 2


def test_external_shutdown_uses_named_event_without_network_endpoint() -> None:
    kernel = FakeKernel()

    assert request_existing_shutdown(kernel=kernel) is True
    assert kernel.event_set is True
    assert len(kernel.closed) == 1

    kernel.event_available = False
    assert request_existing_shutdown(kernel=kernel) is False


def test_wait_for_exit_is_bounded_and_releases_probe_handles() -> None:
    kernel = FakeKernel(mutex_results=[True, True, False])

    assert wait_for_instance_exit(timeout_seconds=1.0, kernel=kernel) is True
    assert len(kernel.closed) == 3
