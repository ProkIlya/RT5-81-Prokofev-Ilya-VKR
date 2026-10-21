"""Раннее пробуждение таймера не означает истечения абсолютного срока."""
import unittest
from unittest.mock import Mock, patch
from api_agent.transport.socket_deadline import SocketDeadline


class SocketDeadlineTests(unittest.TestCase):
    def test_early_callback_rearms_without_interrupting_socket(self):
        connection = Mock()
        with patch('api_agent.transport.socket_deadline.time.monotonic', return_value=10) as clock, \
                patch('api_agent.transport.socket_deadline.threading.Timer') as timer:
            with SocketDeadline(connection, 11) as deadline:
                clock.return_value = 10.99
                deadline._interrupt()
                self.assertFalse(deadline.expired)
                connection.shutdown.assert_not_called()
                self.assertEqual(timer.call_count, 2)
                self.assertAlmostEqual(timer.call_args.args[0], 0.01)
                clock.return_value = 11
                deadline._interrupt()
                self.assertTrue(deadline.expired)
                connection.shutdown.assert_called_once()

    def test_callback_after_exit_cannot_rearm_or_interrupt(self):
        connection = Mock()
        with patch('api_agent.transport.socket_deadline.time.monotonic', return_value=10) as clock, \
                patch('api_agent.transport.socket_deadline.threading.Timer') as timer:
            with SocketDeadline(connection, 11) as deadline:
                pass
            clock.return_value = 12
            deadline._interrupt()
            self.assertFalse(deadline.expired)
            connection.shutdown.assert_not_called()
            self.assertEqual(timer.call_count, 1)
