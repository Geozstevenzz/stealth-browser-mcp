"""Startup must not kill browsers recorded by current or concurrent sessions."""

import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
with patch("threading.Thread.start"), patch("atexit.register"), patch("signal.signal"):
    ProcessCleanup = importlib.import_module("process_cleanup").ProcessCleanup


class StartupCleanupTest(unittest.TestCase):
    def test_shared_registry_never_authorizes_startup_termination(self):
        cleanup = ProcessCleanup.__new__(ProcessCleanup)
        cleanup._load_tracked_pids = Mock(return_value={
            "just-launched": {"pid": 100, "user_data_dir": "shared-profile"},
            "another-session": {"pid": 200, "user_data_dir": "other-profile"},
        })
        cleanup._kill_processes_for_metadata = Mock()
        cleanup._cleanup_profile_for_metadata = Mock()
        cleanup._clear_pid_file = Mock()
        cleanup._sweep_orphaned_temp_profiles = Mock()

        cleanup._recover_orphaned_processes()

        cleanup._kill_processes_for_metadata.assert_not_called()
        cleanup._cleanup_profile_for_metadata.assert_not_called()
        cleanup._clear_pid_file.assert_not_called()
        cleanup._sweep_orphaned_temp_profiles.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
