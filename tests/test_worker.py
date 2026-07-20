import sys
import unittest
from unittest.mock import patch

from backend.worker import main, worker_healthcheck


class WorkerHealthcheckTests(unittest.TestCase):
    @patch("backend.worker.worker_heartbeat_status")
    def test_healthcheck_reflects_fresh_worker_heartbeat(self, heartbeat_status):
        heartbeat_status.return_value = {"healthy": True}

        self.assertTrue(worker_healthcheck())

        heartbeat_status.return_value = {"healthy": False}
        self.assertFalse(worker_healthcheck())

    @patch("backend.worker.worker_heartbeat_status", side_effect=RuntimeError("db unavailable"))
    def test_healthcheck_fails_closed_when_status_cannot_be_read(self, _heartbeat_status):
        self.assertFalse(worker_healthcheck())

    @patch("backend.worker.init_db")
    @patch("backend.worker.worker_healthcheck", return_value=True)
    def test_healthcheck_mode_does_not_initialize_or_run_worker(
        self,
        _worker_healthcheck,
        init_db,
    ):
        with patch.object(sys, "argv", ["backend.worker", "--healthcheck"]):
            with self.assertRaises(SystemExit) as exit_context:
                main()

        self.assertEqual(exit_context.exception.code, 0)
        init_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
