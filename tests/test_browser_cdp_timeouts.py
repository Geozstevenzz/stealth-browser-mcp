"""Normal browsing needs no Fetch pauses; script calls must have a deadline."""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dynamic_hook_system import DynamicHookSystem
from dom_handler import DOMHandler


class BrowserCdpTimeoutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hooks = DynamicHookSystem()
        self.commands = []

        async def send(command):
            self.commands.append(next(command))
        self.tab = SimpleNamespace(send=AsyncMock(side_effect=send), handlers={})

        def add_handler(event, callback):
            self.tab.handlers.setdefault(event, []).append(callback)
        self.tab.add_handler = Mock(side_effect=add_handler)

    async def test_zero_hooks_do_not_pause_any_request(self):
        await self.hooks.setup_interception(self.tab, "owned")
        self.tab.send.assert_not_awaited()
        self.tab.add_handler.assert_not_called()

    async def test_late_scoped_hook_enables_once_and_removal_disables(self):
        await self.hooks.setup_interception(self.tab, "owned")
        hook_id = await self.hooks.create_hook(
            "target", {"url_pattern": "https://example.test/*"},
            "def process_request(request):\n    return HookAction(action='continue')",
            instance_ids=["owned"],
        )
        self.assertEqual(self.commands[-1]["method"], "Fetch.enable")
        self.assertEqual(self.commands[-1]["params"]["patterns"][0]["urlPattern"], "https://example.test/*")
        await self.hooks.setup_interception(self.tab, "owned")
        self.tab.add_handler.assert_called_once()
        other_tab = SimpleNamespace(send=AsyncMock(), add_handler=Mock())
        await self.hooks.setup_interception(other_tab, "other")
        other_tab.send.assert_not_awaited()

        self.assertTrue(await self.hooks.remove_hook(hook_id))
        self.assertEqual(self.commands[-1]["method"], "Fetch.disable")
        self.assertFalse(self.tab.handlers)

    async def test_global_hook_applies_to_subsequently_registered_instance(self):
        hook_id = await self.hooks.create_hook(
            "global", {"url_pattern": "https://example.test/*"},
            "def process_request(request):\n    return HookAction(action='continue')",
        )
        await self.hooks.setup_interception(self.tab, "later")
        self.assertIn(hook_id, self.hooks.instance_hooks["later"])
        self.assertEqual(self.commands[-1]["method"], "Fetch.enable")

    async def test_unresponsive_evaluation_returns_explicit_timeout(self):
        async def never_returns(script):
            await asyncio.Event().wait()
        tab = SimpleNamespace(evaluate=AsyncMock(side_effect=never_returns))
        with patch.object(DOMHandler, "SCRIPT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(TimeoutError, "browser did not respond"):
                await DOMHandler.execute_script(tab, "location.pathname")

    async def test_normal_evaluation_preserves_result(self):
        tab = SimpleNamespace(evaluate=AsyncMock(return_value="/home"))
        self.assertEqual(await DOMHandler.execute_script(tab, "location.pathname"), "/home")


if __name__ == "__main__":
    unittest.main()
