# -*- coding: utf-8 -*-
import ast
import asyncio
import contextlib
import datetime
import io
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MAIN_PATH = ROOT / "main.py"


def _module_tree():
    return ast.parse(MAIN_PATH.read_text(encoding="utf-8-sig"))


def _constant_value(tree, name):
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing module constant: {name}")


def _extract_function(tree, name):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace = {
                "asyncio": asyncio,
                "datetime": datetime,
                "time": time,
                "SLEEP_RESUME_THRESHOLD_SEC": 60,
            }
            exec(compile(module, str(MAIN_PATH), "exec"), namespace)
            return namespace[name]
    raise AssertionError(f"missing function: {name}")


def _calls_named(nodes, attribute_name):
    if not isinstance(nodes, list):
        nodes = [nodes]
    calls = []
    for root in nodes:
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attribute_name
            ):
                calls.append(node)
    return calls


def _attributes_named(nodes, attribute_name):
    if not isinstance(nodes, list):
        nodes = [nodes]
    return [
        node
        for root in nodes
        for node in ast.walk(root)
        if isinstance(node, ast.Attribute) and node.attr == attribute_name
    ]


class PeriodicDeadlineTest(unittest.TestCase):
    def setUp(self):
        tree = _module_tree()
        self.advance = _extract_function(tree, "_advance_periodic_deadline")

    def test_on_time_cycle_advances_one_anchored_slot(self):
        self.assertEqual(11.0, self.advance(10.0, 1.0, 10.2))
        self.assertEqual(13.0, self.advance(10.0, 3.0, 10.2))

    def test_overrun_skips_missed_slots_without_drift_or_backlog(self):
        deadline = 10.0
        deadline = self.advance(deadline, 1.0, 10.2)
        self.assertEqual(11.0, deadline)

        deadline = self.advance(deadline, 1.0, 14.4)
        self.assertEqual(15.0, deadline)

        deadline = self.advance(deadline, 1.0, 15.1)
        self.assertEqual(16.0, deadline)

    def test_exactly_missed_deadline_is_not_replayed(self):
        self.assertEqual(12.0, self.advance(10.0, 1.0, 11.0))

    def test_early_check_keeps_existing_deadline(self):
        self.assertEqual(10.0, self.advance(10.0, 1.0, 9.5))

    def test_invalid_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            self.advance(10.0, 0.0, 10.0)


class WarmupRetryTest(unittest.TestCase):
    def setUp(self):
        tree = _module_tree()
        self.due = _extract_function(tree, "_warmup_due")
        self.schedule = _extract_function(tree, "_schedule_warmup_retry")

    def test_failed_maintenance_waits_until_retry_deadline(self):
        self.assertFalse(self.due(100.0, 130.0, 101.0, 43200.0))
        self.assertTrue(self.due(100.0, 130.0, 130.0, 43200.0))

    def test_successful_maintenance_uses_long_cadence(self):
        self.assertFalse(self.due(100.0, 0.0, 101.0, 43200.0))
        self.assertTrue(self.due(100.0, 0.0, 43600.0, 43200.0))

    def test_retry_delay_is_exponential_but_bounded(self):
        deadline, next_delay = self.schedule(100.0, 30.0, 30.0, 300.0)
        self.assertEqual(130.0, deadline)
        self.assertEqual(60.0, next_delay)

        deadline, next_delay = self.schedule(200.0, 600.0, 30.0, 300.0)
        self.assertEqual(500.0, deadline)
        self.assertEqual(300.0, next_delay)


class MaintenanceLaneTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = _module_tree()
        self.run_cycle = _extract_function(tree, "_run_maintenance_cycle")
        self.due = _extract_function(tree, "_warmup_due")
        self.schedule = _extract_function(tree, "_schedule_warmup_retry")
        self.start_if_due = _extract_function(
            tree,
            "_start_maintenance_task_if_due",
        )
        self.reap = _extract_function(tree, "_reap_finished_maintenance_task")
        self.cancel_and_reap = _extract_function(
            tree,
            "_cancel_and_reap_maintenance_task",
        )

    async def test_slow_maintenance_does_not_block_fast_tick_or_overlap(self):
        started = asyncio.Event()
        release = asyncio.Event()
        fast_ticks = 0
        warmup_calls = 0
        retention_calls = 0

        async def fast_tick_probe():
            nonlocal fast_ticks
            for _ in range(3):
                await asyncio.sleep(0)
                fast_ticks += 1

        async def slow_warmup(pool):
            nonlocal warmup_calls
            warmup_calls += 1
            started.set()
            await release.wait()

        async def retention(pool):
            nonlocal retention_calls
            retention_calls += 1
            return True

        task = self.start_if_due(
            None,
            True,
            True,
            lambda: asyncio.create_task(
                self.run_cycle(object(), slow_warmup, retention)
            ),
        )
        await started.wait()
        await asyncio.wait_for(fast_tick_probe(), timeout=1.0)
        self.assertFalse(task.done())
        self.assertEqual(3, fast_ticks)
        self.assertIs(
            task,
            self.start_if_due(
                task,
                True,
                True,
                lambda: self.fail("maintenance lane must remain single-flight"),
            ),
        )
        self.assertEqual(1, warmup_calls)
        self.assertEqual(0, retention_calls)

        release.set()
        self.assertTrue(await asyncio.wait_for(task, timeout=1.0))
        self.assertEqual(1, retention_calls)

    async def test_maintenance_failure_result_is_reaped_without_waiting(self):
        async def warmup(pool):
            raise RuntimeError("warmup failed")

        async def retention(pool):
            return False

        task = asyncio.create_task(
            self.run_cycle(object(), warmup, retention)
        )
        selected, error, succeeded, finished = self.reap(task)
        self.assertIs(task, selected)
        self.assertIsNone(error)
        self.assertFalse(succeeded)
        self.assertFalse(finished)

        self.assertFalse(await task)
        selected, error, succeeded, finished = self.reap(task)
        self.assertIsNone(selected)
        self.assertIsNone(error)
        self.assertFalse(succeeded)
        self.assertTrue(finished)

        retry_deadline, next_delay = self.schedule(100.0, 30.0, 30.0, 300.0)
        self.assertFalse(self.due(100.0, retry_deadline, 100.1, 43200.0))
        self.assertEqual(60.0, next_delay)

    async def test_shutdown_cancels_and_reaps_running_maintenance(self):
        entered = asyncio.Event()

        async def slow_maintenance():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(slow_maintenance())
        await entered.wait()
        await self.cancel_and_reap(task)
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())


class SleepResumeObservationTest(unittest.TestCase):
    def setUp(self):
        self.observe = _extract_function(
            _module_tree(),
            "_observe_sleep_resume",
        )

    def test_long_wall_gap_invokes_resume_handler_and_advances_anchor(self):
        handled = []

        observed, detected = self.observe(
            100.0,
            lambda before, after: handled.append((before, after)),
            wall_clock=lambda: 161.0,
            sleep_threshold=60.0,
        )

        self.assertEqual(161.0, observed)
        self.assertTrue(detected)
        self.assertEqual([(100.0, 161.0)], handled)

    def test_short_wall_gap_only_advances_anchor(self):
        handled = []

        observed, detected = self.observe(
            100.0,
            lambda before, after: handled.append((before, after)),
            wall_clock=lambda: 100.9,
            sleep_threshold=60.0,
        )

        self.assertEqual(100.9, observed)
        self.assertFalse(detected)
        self.assertEqual([], handled)


class _FakeTask:
    def __init__(self, *, done=False, result=None, error=None):
        self._done = done
        self._result = result
        self._error = error
        self.result_calls = 0

    def done(self):
        return self._done

    def result(self):
        self.result_calls += 1
        if self._error is not None:
            raise self._error
        return self._result


class ActivityLaneStateTest(unittest.TestCase):
    def setUp(self):
        tree = _module_tree()
        self.start_if_due = _extract_function(tree, "_start_activity_task_if_due")
        self.reap_finished = _extract_function(tree, "_reap_finished_activity_task")

    def test_running_activity_task_prevents_overlap_and_backlog(self):
        running = _FakeTask(done=False)
        starts = []

        selected = self.start_if_due(
            running,
            activity_due=True,
            lane_available=True,
            task_factory=lambda: starts.append("started"),
        )

        self.assertIs(running, selected)
        self.assertEqual([], starts)

    def test_activity_task_starts_once_only_when_slot_is_due_and_lane_is_free(self):
        starts = []
        created = object()

        self.assertIsNone(
            self.start_if_due(
                None,
                activity_due=False,
                lane_available=True,
                task_factory=lambda: starts.append(created),
            )
        )
        selected = self.start_if_due(
            None,
            activity_due=True,
            lane_available=True,
            task_factory=lambda: starts.append(created) or created,
        )

        self.assertIs(created, selected)
        self.assertEqual([created], starts)

    def test_cancelled_to_thread_residue_is_not_queued_behind_busy_lock(self):
        starts = []

        selected = self.start_if_due(
            None,
            activity_due=True,
            lane_available=False,
            task_factory=lambda: starts.append("started"),
        )

        self.assertIsNone(selected)
        self.assertEqual([], starts)

    def test_reap_is_nonblocking_for_running_task_and_consumes_finished_result(self):
        running = _FakeTask(done=False)
        selected, error, succeeded = self.reap_finished(running)
        self.assertIs(running, selected)
        self.assertIsNone(error)
        self.assertFalse(succeeded)
        self.assertEqual(0, running.result_calls)

        finished = _FakeTask(done=True, result="ok")
        selected, error, succeeded = self.reap_finished(finished)
        self.assertIsNone(selected)
        self.assertIsNone(error)
        self.assertTrue(succeeded)
        self.assertEqual(1, finished.result_calls)

    def test_reap_returns_finished_error_without_restarting_lane(self):
        expected = RuntimeError("activity failed")
        finished = _FakeTask(done=True, error=expected)

        selected, error, succeeded = self.reap_finished(finished)

        self.assertIsNone(selected)
        self.assertIs(expected, error)
        self.assertFalse(succeeded)

    def test_reap_does_not_renew_health_for_a_discarded_cross_sleep_sample(self):
        discarded = _FakeTask(done=True, result=False)

        selected, error, succeeded = self.reap_finished(discarded)

        self.assertIsNone(selected)
        self.assertIsNone(error)
        self.assertFalse(succeeded)


class HealthLeaseTest(unittest.TestCase):
    def setUp(self):
        tree = _module_tree()
        self.lease_is_current = _extract_function(tree, "_health_lease_is_current")
        self.should_refresh = _extract_function(
            tree,
            "_should_refresh_telemetry_heartbeat",
        )
        self.update_warning = _extract_function(tree, "_update_health_warning")
        self.tree = tree

    def test_activity_lane_gets_bounded_initial_grace_then_stales(self):
        grace = _constant_value(
            self.tree,
            "ACTIVITY_HEALTH_INITIAL_GRACE_SEC",
        )
        max_age = _constant_value(self.tree, "ACTIVITY_HEALTH_MAX_AGE_SEC")

        self.assertEqual(30.0, grace)
        self.assertEqual(30.0, max_age)
        self.assertLess(max_age, 90.0)
        self.assertTrue(
            self.lease_is_current(100.0, None, 130.0, grace, max_age)
        )
        self.assertFalse(
            self.lease_is_current(100.0, None, 130.001, grace, max_age)
        )

    def test_success_renews_activity_lease_but_permanent_stall_stops_heartbeat(self):
        self.assertTrue(
            self.lease_is_current(0.0, 12.0, 42.0, 30.0, 30.0)
        )
        activity_healthy = self.lease_is_current(
            0.0,
            12.0,
            42.001,
            30.0,
            30.0,
        )

        self.assertFalse(activity_healthy)
        self.assertFalse(self.should_refresh(True, True, activity_healthy))

    def test_single_context_failure_remains_inside_lease(self):
        max_age = _constant_value(self.tree, "CONTEXT_HEALTH_MAX_AGE_SEC")
        self.assertEqual(15.0, max_age)

        context_healthy = self.lease_is_current(
            0.0,
            20.0,
            21.0,
            15.0,
            max_age,
        )
        self.assertTrue(context_healthy)
        self.assertTrue(self.should_refresh(True, context_healthy, True))
        self.assertFalse(
            self.lease_is_current(0.0, 20.0, 35.001, 15.0, max_age)
        )

    def test_heartbeat_requires_hardware_context_and_activity_health(self):
        self.assertTrue(self.should_refresh(True, True, True))
        for health in (
            (False, True, True),
            (True, False, True),
            (True, True, False),
        ):
            with self.subTest(health=health):
                self.assertFalse(self.should_refresh(*health))

    def test_stale_warning_is_payload_free_and_emitted_once_per_transition(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            warning_active = self.update_warning("活动慢车道", False, False)
            warning_active = self.update_warning(
                "活动慢车道",
                False,
                warning_active,
            )

        self.assertTrue(warning_active)
        self.assertEqual(1, output.getvalue().count("健康租约已过期"))
        self.assertNotIn("PID", output.getvalue())
        self.assertNotIn("window", output.getvalue().lower())


class ActivityLaneShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_pre_sleep_snapshot_never_reaches_activity_write(self):
        run_activity = _extract_function(
            _module_tree(),
            "_collect_and_write_activity_snapshot",
        )
        collection_started = threading.Event()
        collection_finished = threading.Event()
        release_collection = threading.Event()

        class SlowWorker:
            def __init__(self):
                self.write_calls = 0

            def collect_active_processes(self):
                collection_started.set()
                try:
                    if not release_collection.wait(timeout=2.0):
                        raise TimeoutError("test did not release pre-sleep collection")
                    return ["pre-sleep-snapshot"]
                finally:
                    collection_finished.set()

            async def write_batch_to_db(self, pool, rows, timestamp):
                self.write_calls += 1

        worker = SlowWorker()
        task = asyncio.create_task(
            run_activity(
                worker,
                object(),
                datetime.datetime(2026, 8, 8, tzinfo=datetime.timezone.utc),
                asyncio.Event(),
            )
        )
        try:
            for _ in range(200):
                if collection_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(collection_started.is_set())

            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            # The fast lane does not wait for the non-cancellable worker thread.
            self.assertFalse(collection_finished.is_set())
            self.assertTrue(task.cancelled())
        finally:
            release_collection.set()

        for _ in range(200):
            if collection_finished.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(collection_finished.is_set())
        self.assertEqual(0, worker.write_calls)

    async def test_collection_that_spans_sleep_is_discarded_before_database_write(self):
        run_activity = _extract_function(
            _module_tree(),
            "_collect_and_write_activity_snapshot",
        )

        class Worker:
            def __init__(self):
                self.write_calls = 0

            def collect_active_processes(self):
                return ["cross-sleep-snapshot"]

            async def write_batch_to_db(self, pool, rows, timestamp):
                self.write_calls += 1

        wall_times = iter((100.0, 161.0))
        worker = Worker()
        hardware_committed = asyncio.Event()
        hardware_committed.set()
        result = await run_activity(
            worker,
            object(),
            datetime.datetime(2026, 8, 8, tzinfo=datetime.timezone.utc),
            hardware_committed,
            wall_clock=lambda: next(wall_times),
            sleep_threshold=60.0,
        )

        self.assertFalse(result)
        self.assertEqual(0, worker.write_calls)

    async def test_activity_waits_for_hardware_commit_and_preserves_timestamp_identity(self):
        run_activity = _extract_function(
            _module_tree(),
            "_collect_and_write_activity_snapshot",
        )

        class Worker:
            def __init__(self):
                self.written_timestamp = None

            def collect_active_processes(self):
                return ["snapshot"]

            async def write_batch_to_db(self, pool, rows, timestamp):
                self.written_timestamp = timestamp

        worker = Worker()
        hardware_committed = asyncio.Event()
        sample_timestamp = object()
        task = asyncio.create_task(
            run_activity(
                worker,
                object(),
                sample_timestamp,
                hardware_committed,
            )
        )
        await asyncio.sleep(0.02)
        self.assertIsNone(worker.written_timestamp)

        hardware_committed.set()
        self.assertTrue(await asyncio.wait_for(task, timeout=1.0))
        self.assertIs(sample_timestamp, worker.written_timestamp)

    async def test_slow_activity_does_not_block_fast_work_or_spawn_overlap(self):
        tree = _module_tree()
        run_activity = _extract_function(
            tree,
            "_collect_and_write_activity_snapshot",
        )
        start_if_due = _extract_function(tree, "_start_activity_task_if_due")
        collection_started = threading.Event()
        release_collection = threading.Event()

        class SlowWorker:
            def __init__(self):
                self.collect_calls = 0
                self.write_calls = 0

            def collect_active_processes(self):
                self.collect_calls += 1
                collection_started.set()
                if not release_collection.wait(timeout=2.0):
                    raise TimeoutError("test did not release slow activity")
                return ["snapshot"]

            async def write_batch_to_db(self, pool, rows, timestamp):
                self.write_calls += 1

        worker = SlowWorker()
        starts = []

        def start_activity():
            task = asyncio.create_task(
                run_activity(
                    worker,
                    object(),
                    datetime.datetime(2026, 8, 8, tzinfo=datetime.timezone.utc),
                    hardware_committed,
                )
            )
            starts.append(task)
            return task

        hardware_committed = asyncio.Event()
        task = start_if_due(None, True, True, start_activity)
        try:
            for _ in range(200):
                if collection_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(collection_started.is_set())

            # A representative fast-lane await completes while the slow process
            # scan is still blocked in its worker thread.
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
            self.assertFalse(task.done())

            selected = start_if_due(
                task,
                True,
                True,
                lambda: self.fail("a running activity lane must not overlap"),
            )
            self.assertIs(task, selected)
            self.assertEqual(1, len(starts))
        finally:
            release_collection.set()

        hardware_committed.set()
        await asyncio.wait_for(task, timeout=1.0)
        self.assertEqual(1, worker.collect_calls)
        self.assertEqual(1, worker.write_calls)

    async def test_shutdown_cancels_and_reaps_running_task(self):
        shutdown = _extract_function(
            _module_tree(),
            "_cancel_and_reap_activity_task",
        )
        entered = asyncio.Event()

        async def slow_activity():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(slow_activity())
        await entered.wait()

        await shutdown(task)

        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())


class HardwareForegroundRefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_context_poll_does_not_freeze_hardware_on_old_pid(self):
        sample_identity = _extract_function(
            _module_tree(),
            "_sample_current_foreground_identity",
        )

        class DelayedTracker:
            def __init__(self):
                self.current_pid = 111
                self.last_pid = None
                self.poll_started = asyncio.Event()
                self.release_poll = asyncio.Event()

            async def poll_heartbeat(self):
                captured_pid = self.current_pid
                self.poll_started.set()
                await self.release_poll.wait()
                self.last_pid = captured_pid

            def check_foreground_window_fast(self):
                return {"os_pid": self.current_pid}

        tracker = DelayedTracker()
        poll_task = asyncio.create_task(tracker.poll_heartbeat())
        await tracker.poll_started.wait()
        tracker.current_pid = 222
        tracker.release_poll.set()
        await poll_task

        self.assertEqual(111, tracker.last_pid)
        hardware_pid, app_name = sample_identity(
            tracker,
            lambda pid: f"process-{pid}",
        )
        self.assertEqual(222, hardware_pid)
        self.assertEqual("process-222", app_name)


class CollectorCadenceWiringTest(unittest.TestCase):
    def setUp(self):
        self.tree = _module_tree()
        self.collector = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_collector"
        )
        self.activity_lane = next(
            node
            for node in self.tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_collect_and_write_activity_snapshot"
        )

    def test_declares_one_second_fast_and_three_second_activity_periods(self):
        self.assertEqual(1.0, _constant_value(self.tree, "TELEMETRY_INTERVAL_SEC"))
        self.assertEqual(3.0, _constant_value(self.tree, "ACTIVITY_INTERVAL_SEC"))

    def test_only_background_lane_owns_expensive_activity_pipeline(self):
        hardware_calls = _calls_named(self.collector, "collect_hardware_snapshot")
        hardware_writes = _calls_named(self.collector, "write_to_db")
        context_calls = _calls_named(self.collector, "poll_heartbeat")

        self.assertEqual(1, len(hardware_calls))
        self.assertEqual(1, len(hardware_writes))
        self.assertEqual(1, len(context_calls))

        self.assertFalse(_attributes_named(self.collector, "collect_active_processes"))
        self.assertFalse(_calls_named(self.collector, "write_batch_to_db"))
        self.assertEqual(
            1,
            len(_attributes_named(self.activity_lane, "collect_active_processes")),
        )
        self.assertEqual(1, len(_calls_named(self.activity_lane, "write_batch_to_db")))

    def test_slow_activity_lane_is_not_awaited_by_fast_collector(self):
        parent = {}
        for node in ast.walk(self.collector):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        lane_calls = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_collect_and_write_activity_snapshot"
        ]
        self.assertEqual(1, len(lane_calls))

        node = lane_calls[0]
        while node in parent:
            node = parent[node]
            self.assertNotIsInstance(node, ast.Await)

        awaited_names = {
            node.value.id
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Name)
        }
        self.assertIn("hardware_write_task", awaited_names)
        self.assertNotIn("activity_task", awaited_names)

        self.assertFalse(
            _attributes_named(self.collector, "collect_active_processes"),
            "the expensive process scan must live only in the background lane",
        )

    def test_resume_path_cancels_pre_sleep_activity_without_awaiting_it(self):
        resume_handler = next(
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_handle_sleep_resume"
        )
        cancel_calls = [
            node
            for node in _calls_named(resume_handler.body, "cancel")
            if isinstance(node.func.value, ast.Name)
            and node.func.value.id == "activity_task"
        ]

        self.assertEqual(1, len(cancel_calls))
        resume_nodes = {
            id(node)
            for root in resume_handler.body
            for node in ast.walk(root)
        }
        for node in ast.walk(resume_handler):
            if isinstance(node, ast.Await):
                self.assertNotIn(id(node.value), {id(call) for call in cancel_calls})
        self.assertIn(id(cancel_calls[0]), resume_nodes)
        self.assertEqual(1, len(_calls_named(resume_handler.body, "mark_sleep_boundary")))

        assignments = {
            target.id: node.value
            for root in resume_handler.body
            for node in ast.walk(root)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id in {
                "health_started_at",
                "activity_last_success_at",
                "context_last_success_at",
            }
        }
        self.assertEqual(
            {
                "health_started_at",
                "activity_last_success_at",
                "context_last_success_at",
            },
            set(assignments),
        )
        self.assertIsInstance(assignments["activity_last_success_at"], ast.Constant)
        self.assertIsNone(assignments["activity_last_success_at"].value)
        self.assertIsInstance(assignments["context_last_success_at"], ast.Constant)
        self.assertIsNone(assignments["context_last_success_at"].value)

    def test_both_deadlines_advance_from_their_existing_anchor(self):
        advance_calls = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_advance_periodic_deadline"
        ]
        first_argument_names = {
            node.args[0].id
            for node in advance_calls
            if node.args and isinstance(node.args[0], ast.Name)
        }

        self.assertIn("next_telemetry_deadline", first_argument_names)
        self.assertIn("next_activity_deadline", first_argument_names)

    def test_hardware_and_activity_share_post_context_sample_timestamp(self):
        poll_call = _calls_named(self.collector, "poll_heartbeat")[0]
        collect_call = _calls_named(self.collector, "collect_hardware_snapshot")[0]
        write_call = _calls_named(self.collector, "write_to_db")[0]
        activity_call = next(
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_collect_and_write_activity_snapshot"
        )

        identity_calls = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_sample_current_foreground_identity"
        ]
        self.assertEqual(1, len(identity_calls))

        sample_assignments = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id == "sample_timestamp"
        ]

        self.assertEqual(1, len(sample_assignments))
        sample_assignment = sample_assignments[0]
        self.assertLess(poll_call.lineno, identity_calls[0].lineno)
        self.assertLess(identity_calls[0].lineno, sample_assignment.lineno)
        self.assertLess(
            sample_assignment.lineno,
            collect_call.lineno,
        )
        self.assertIsInstance(collect_call.args[1], ast.Name)
        self.assertEqual("hardware_pid", collect_call.args[1].id)
        self.assertIsInstance(write_call.args[2], ast.Name)
        self.assertEqual("sample_timestamp", write_call.args[2].id)
        self.assertEqual(
            "datetime.datetime.now(datetime.timezone.utc)",
            ast.unparse(sample_assignment.value),
        )
        self.assertGreaterEqual(len(activity_call.args), 4)
        self.assertIsInstance(activity_call.args[2], ast.Name)
        self.assertEqual("sample_timestamp", activity_call.args[2].id)
        self.assertIsInstance(activity_call.args[3], ast.Name)
        self.assertEqual("hardware_committed", activity_call.args[3].id)

    def test_activity_lane_uses_the_shared_sample_timestamp(self):
        timestamp_assignments = [
            node
            for node in ast.walk(self.activity_lane)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "activity_sample_timestamp"
                for target in node.targets
            )
        ]
        self.assertEqual(0, len(timestamp_assignments))
        parameter_names = [argument.arg for argument in self.activity_lane.args.args]
        self.assertIn("sample_timestamp", parameter_names)

        sample_reassignments = [
            node
            for node in ast.walk(self.activity_lane)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr))
            for target in (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            if isinstance(target, ast.Name) and target.id == "sample_timestamp"
        ]
        self.assertEqual([], sample_reassignments)

        collect_call = _calls_named(self.activity_lane, "to_thread")[0]
        write_call = _calls_named(self.activity_lane, "write_batch_to_db")[0]
        self.assertLess(collect_call.lineno, write_call.lineno)
        self.assertIsInstance(write_call.args[2], ast.Name)
        self.assertEqual("sample_timestamp", write_call.args[2].id)

    def test_resume_is_rechecked_after_hardware_before_heartbeat(self):
        observe_calls = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_observe_sleep_resume"
        ]
        self.assertGreaterEqual(len(observe_calls), 2)

        hardware_awaits = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Name)
            and node.value.id == "hardware_write_task"
        ]
        heartbeat_call = next(
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "write_telemetry_heartbeat"
        )
        self.assertEqual(1, len(hardware_awaits))
        post_hardware_checks = [
            call
            for call in observe_calls
            if hardware_awaits[0].lineno < call.lineno < heartbeat_call.lineno
        ]
        self.assertTrue(post_hardware_checks)

        parent = {}
        for node in ast.walk(self.collector):
            for child in ast.iter_child_nodes(node):
                parent[child] = node
        guarding_if = None
        node = heartbeat_call
        while node in parent:
            node = parent[node]
            if isinstance(node, ast.If):
                guarding_if = node
                break
        self.assertIsNotNone(guarding_if)
        self.assertIn("resume_detected_this_slot", ast.unparse(guarding_if.test))

    def test_wall_anchor_is_never_raw_overwritten_inside_the_loop(self):
        raw_wall_assignments = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "wall_anchor"
                for target in node.targets
            )
            and ast.unparse(node.value) == "time.time()"
        ]

        self.assertEqual(
            1,
            len(raw_wall_assignments),
            "only the pre-loop initialization may assign wall_anchor directly",
        )

    def test_total_heartbeat_write_is_guarded_by_all_health_inputs(self):
        heartbeat_calls = [
            node
            for node in ast.walk(self.collector)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "write_telemetry_heartbeat"
        ]
        self.assertEqual(1, len(heartbeat_calls))

        parent = {}
        for node in ast.walk(self.collector):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        node = heartbeat_calls[0]
        guarding_if = None
        while node in parent:
            node = parent[node]
            if isinstance(node, ast.If):
                gate_calls = [
                    call
                    for call in ast.walk(node.test)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_should_refresh_telemetry_heartbeat"
                ]
                if gate_calls:
                    guarding_if = node
                    break

        self.assertIsNotNone(guarding_if)

    def test_maintenance_lane_has_bounded_retry_and_retention_result(self):
        source = ast.unparse(self.collector)
        self.assertIn("_warmup_due", source)
        self.assertIn("_schedule_warmup_retry", source)
        self.assertIn("warmup_retry_deadline_wall", source)
        self.assertIn("_run_maintenance_cycle", source)
        self.assertIn("_reap_finished_maintenance_task", source)
        self.assertIn("_start_maintenance_task_if_due", source)

        tree = _module_tree()
        maintenance = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_run_maintenance_cycle"
        )
        maintenance_source = ast.unparse(maintenance)
        self.assertIn("retention_ok = await retention_runner(pool)", maintenance_source)

    def test_maintenance_result_is_processed_before_next_start_decision(self):
        def calls_to(name):
            return [
                node
                for node in ast.walk(self.collector)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == name
            ]

        reap_calls = calls_to("_reap_finished_maintenance_task")
        retry_calls = calls_to("_schedule_warmup_retry")
        due_calls = calls_to("_warmup_due")
        start_calls = calls_to("_start_maintenance_task_if_due")

        self.assertEqual(1, len(reap_calls))
        self.assertEqual(1, len(retry_calls))
        self.assertEqual(1, len(due_calls))
        self.assertEqual(1, len(start_calls))
        self.assertLess(reap_calls[0].lineno, retry_calls[0].lineno)
        self.assertLess(retry_calls[0].lineno, due_calls[0].lineno)
        self.assertLess(due_calls[0].lineno, start_calls[0].lineno)


if __name__ == "__main__":
    unittest.main()
