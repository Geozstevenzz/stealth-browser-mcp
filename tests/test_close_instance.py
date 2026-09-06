"""Closing must verify process exit before forgetting an owned browser."""

import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
with patch("threading.Thread.start"), patch("atexit.register"), patch("signal.signal"), \
        patch("psutil.process_iter", return_value=[]), patch("pathlib.Path.glob", return_value=[]):
    module = importlib.import_module("browser_manager")


class CloseInstanceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.order = []
        self.process = SimpleNamespace(
            returncode=None, pid=123, terminate=Mock(), kill=Mock(), wait=AsyncMock()
        )
        self.browser = SimpleNamespace(
            _process=self.process,
            _process_pid=123,
            connection=SimpleNamespace(
                send=AsyncMock(side_effect=lambda command: self.order.append("close")),
                disconnect=AsyncMock(side_effect=lambda: self.order.append("disconnect")),
            ),
        )
        self.manager = module.BrowserManager()
        self.instance = SimpleNamespace(state=module.BrowserState.READY)
        self.data = {"browser": self.browser, "instance": self.instance}
        self.manager._instances["owned"] = self.data
        self.cleanup = Mock()
        self.cleanup.browser_processes = {"owned": {"pid": 123}}
        self.cleanup.kill_browser_process.side_effect = lambda instance_id: self.order.append("fallback") or True
        self.cleanup.is_process_alive.return_value = False
        self.cleanup.finalize_browser_process.return_value = True
        self.storage = Mock()
        self.addCleanup(patch.stopall)
        patch.object(module, "process_cleanup", self.cleanup).start()
        patch.object(module, "persistent_storage", self.storage).start()
        patch.object(module, "debug_logger", Mock()).start()

    async def test_browser_close_precedes_disconnect_and_verified_removal(self):
        def exited():
            self.process.returncode = 0
        self.process.terminate.side_effect = exited

        self.assertTrue(await self.manager.close_instance("owned"))

        self.assertEqual(self.order, ["close", "disconnect", "fallback"])
        self.process.wait.assert_awaited()
        self.cleanup.finalize_browser_process.assert_called_once_with("owned")
        self.assertNotIn("owned", self.manager._instances)
        self.assertEqual(self.instance.state, module.BrowserState.CLOSED)
        self.storage.remove_instance.assert_called_once_with("owned")

    async def test_failed_termination_preserves_handles_and_can_be_retried(self):
        self.cleanup.kill_browser_process.return_value = False
        self.cleanup.kill_browser_process.side_effect = None
        self.cleanup.is_process_alive.return_value = True
        self.process.terminate.side_effect = PermissionError("still running")
        self.process.kill.side_effect = PermissionError("still running")

        self.assertFalse(await self.manager.close_instance("owned"))

        self.assertIs(self.manager._instances["owned"], self.data)
        self.assertIs(self.browser._process, self.process)
        self.assertEqual(self.browser._process_pid, 123)
        self.assertEqual(self.instance.state, module.BrowserState.READY)
        self.storage.remove_instance.assert_not_called()
        self.cleanup.finalize_browser_process.assert_not_called()

        self.process.returncode = 0
        self.cleanup.is_process_alive.return_value = False
        self.assertTrue(await self.manager.close_instance("owned"))
        self.assertNotIn("owned", self.manager._instances)

    async def test_exited_owned_custom_browser_skips_slow_profile_sweeps(self):
        self.cleanup.browser_processes["owned"]["uses_custom_data_dir"] = True

        async def graceful_exit(command):
            self.process.returncode = 0
        self.browser.connection.send.side_effect = graceful_exit

        def untrack(instance_id):
            del self.cleanup.browser_processes[instance_id]
            return True
        self.cleanup.untrack_browser_process.side_effect = untrack

        self.assertTrue(await self.manager.close_instance("owned"))

        self.cleanup.kill_browser_process.assert_not_called()
        self.cleanup.is_process_alive.assert_not_called()
        self.cleanup.finalize_browser_process.assert_not_called()
        self.cleanup.untrack_browser_process.assert_called_once_with("owned")
        self.process.terminate.assert_not_called()
        self.assertNotIn("owned", self.manager._instances)

    async def test_grace_wait_observes_delayed_exit_before_process_scan(self):
        self.cleanup.browser_processes["owned"]["uses_custom_data_dir"] = True

        async def exit_after_close():
            self.process.returncode = 0
            return 0
        self.process.wait.side_effect = exit_after_close

        def untrack(instance_id):
            del self.cleanup.browser_processes[instance_id]
            return True
        self.cleanup.untrack_browser_process.side_effect = untrack

        self.assertTrue(await self.manager.close_instance("owned"))

        self.process.wait.assert_awaited_once()
        self.cleanup.kill_browser_process.assert_not_called()
        self.cleanup.is_process_alive.assert_not_called()
        self.cleanup.finalize_browser_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
