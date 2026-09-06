"""Use browser pointer events for controls that ignore DOM click()."""

from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dom_handler import DOMHandler


class ClickElementTest(unittest.IsolatedAsyncioTestCase):
    async def test_mouse_click_precedes_dom_fallback(self):
        for mouse_error in (None, RuntimeError("no visible coordinates")):
            with self.subTest(mouse_error=mouse_error):
                element = AsyncMock()
                element.mouse_click.side_effect = mouse_error
                tab = AsyncMock()
                tab.select.return_value = element
                with patch("dom_handler.asyncio.sleep", new=AsyncMock()):
                    self.assertTrue(await DOMHandler.click_element(tab, "button"))
                element.mouse_click.assert_awaited_once_with()
                if mouse_error:
                    element.click.assert_awaited_once_with()
                else:
                    element.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
