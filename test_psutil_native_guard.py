import os
import time

import pytest

from psutil_native_guard import IsolatedConnectionSampler


@pytest.mark.skipif(os.name != "nt", reason="Windows-only process isolation")
def test_connection_sampler_survives_worker_replacement():
    sampler = IsolatedConnectionSampler(
        interval_seconds=0.05,
        stale_after_seconds=2.0,
        startup_timeout_seconds=2.0,
    )
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            sampler.snapshot()
            status = sampler.status()
            if status["last_success_age_seconds"] is not None:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("isolated psutil worker never produced a snapshot")

        old_pid = sampler.status()["worker_pid"]
        sampler._process.terminate()
        sampler._process.join(timeout=2.0)

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            sampler.snapshot()
            status = sampler.status()
            if (
                status["worker_pid"] != old_pid
                and status["last_success_age_seconds"] is not None
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("isolated psutil worker did not recover")

        assert status["worker_alive"]
        assert status["restart_count"] >= 1
    finally:
        sampler.close()
