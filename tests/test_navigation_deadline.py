"""Navigation milestones and stale-tab recovery share one end-to-end budget."""

import asyncio
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
with patch("threading.Thread.start"), patch("atexit.register"), patch("signal.signal"), \
        patch("psutil.process_iter", return_value=[]), patch("pathlib.Path.glob", return_value=[]):
    module = importlib.import_module("browser_manager")


class NavigationDeadlineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = module.BrowserManager()
        self.manager.touch_instance = AsyncMock(return_value=True)
        self.manager.update_instance_state = AsyncMock()
        self.tab = self.make_tab()
        self.manager.get_navigation_tab = AsyncMock(return_value=self.tab)
        self.manager._replace_main_tab = AsyncMock()
        self.manager._instances["owned"] = {"tab": self.tab, "navigation_count": 0}
        patcher = patch.object(module, "debug_logger", Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_tab(self):
        tab = SimpleNamespace(handlers={})
        tab.add_handler = lambda event, callback: tab.handlers.setdefault(event, []).append(callback)
        tab.evaluate = AsyncMock(return_value=json.dumps({"url": "https://example.test/home", "title": "Home"}))

        async def send(command):
            method = next(command)["method"]
            if method == "Page.navigate":
                self.emit(tab, "main", "current", "DOMContentLoaded")
                return "main", "current", None
        tab.send = AsyncMock(side_effect=send)
        return tab

    def emit(self, tab, frame, loader, name):
        event = SimpleNamespace(frame_id=frame, loader_id=loader, name=name)
        for callback in list(tab.handlers.get(module.uc.cdp.page.LifecycleEvent, [])):
            callback(event)

    async def test_hung_health_lookup_is_inside_deadline(self):
        cancelled = asyncio.Event()

        async def hung_lookup(instance_id):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.manager.get_navigation_tab.side_effect = hung_lookup

        with self.assertRaisesRegex(TimeoutError, "30ms total"):
            await self.manager.navigate("owned", "https://example.test/login", timeout=30)
        self.assertTrue(cancelled.is_set())
        self.tab.send.assert_not_awaited()

    async def test_early_matching_loader_and_redirect_preserve_main_tab(self):
        unrelated_callback = Mock()
        self.tab.add_handler(module.uc.cdp.page.LifecycleEvent, unrelated_callback)

        result = await self.manager.navigate(
            "owned", "https://example.test/login", wait_until="domcontentloaded", timeout=200
        )

        self.assertEqual(result, {"url": "https://example.test/home", "title": "Home", "success": True})
        self.manager.update_instance_state.assert_awaited_once_with("owned", "https://example.test/home", "Home")
        self.assertIs(self.manager._instances["owned"]["tab"], self.tab)
        self.assertEqual(self.manager._instances["owned"]["navigation_count"], 1)
        self.tab.evaluate.assert_awaited_once()
        self.assertEqual(self.tab.handlers[module.uc.cdp.page.LifecycleEvent], [unrelated_callback])

    async def test_unrelated_frame_or_old_loader_does_not_complete_navigation(self):
        async def send(command):
            if next(command)["method"] == "Page.navigate":
                self.emit(self.tab, "iframe", "current", "DOMContentLoaded")
                self.emit(self.tab, "main", "previous", "DOMContentLoaded")
                return "main", "current", None
        self.tab.send.side_effect = send

        with self.assertRaisesRegex(TimeoutError, "30ms total"):
            await self.manager.navigate("owned", "https://example.test/login", wait_until="domcontentloaded", timeout=30)
        self.tab.evaluate.assert_not_awaited()
        self.assertFalse(self.tab.handlers)

    async def test_same_document_navigation_waits_for_ready_state(self):
        async def send(command):
            if next(command)["method"] == "Page.navigate":
                return "main", None, None
        self.tab.send.side_effect = send
        final = {"url": "https://example.test/home#login", "title": "Home"}
        self.tab.evaluate.side_effect = ["loading", "interactive", json.dumps(final)]

        result = await self.manager.navigate("owned", final["url"], wait_until="domcontentloaded", timeout=200)

        self.assertEqual(result["url"], final["url"])
        self.assertEqual(self.tab.evaluate.await_args_list[0].args, ("document.readyState",))
        self.assertEqual(self.tab.evaluate.await_args_list[1].args, ("document.readyState",))

    async def test_recovery_does_not_restart_navigation_budget(self):
        async def first_send(command):
            if next(command)["method"] == "Page.navigate":
                await asyncio.sleep(0.03)
                raise RuntimeError("target closed")
        self.tab.send.side_effect = first_send
        second = self.make_tab()
        cancelled = asyncio.Event()

        async def second_send(command):
            if next(command)["method"] == "Page.navigate":
                try:
                    await asyncio.sleep(0.04)
                    self.emit(second, "main", "current", "DOMContentLoaded")
                    return "main", "current", None
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        second.send.side_effect = second_send
        self.manager._replace_main_tab.return_value = second

        with self.assertRaisesRegex(TimeoutError, "50ms total"):
            await self.manager.navigate("owned", "https://example.test/login", wait_until="domcontentloaded", timeout=50)
        self.manager._replace_main_tab.assert_awaited_once()
        self.assertTrue(cancelled.is_set())
        second.evaluate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
