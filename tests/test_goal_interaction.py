"""Уточнения/approval/private input не расширяют принятый смысл или права."""
import importlib.util
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]


class InteractionTests(unittest.TestCase):
    def cls(self):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.interaction'), 'Нет application ask/private input')
        from api_agent.interaction import HumanInteraction
        return HumanInteraction

    def test_phase_budgets_are_separate_and_approval_does_not_consume_ask(self):
        human = self.cls()(lambda request: {'request_id': request['id'], 'value': 'yes'})
        for _ in range(3): self.assertEqual(human.ask('q', phase='preparation'), 'yes')
        with self.assertRaisesRegex(ValueError, 'ask_limit'): human.ask('q', phase='preparation')
        self.assertEqual(human.approve({'action_hash': 'h', 'config_version': 1})['approved'], True)
        for _ in range(3): human.ask('q', phase='run')
        with self.assertRaisesRegex(ValueError, 'ask_limit'): human.ask('q', phase='run')

    def test_timeout_cancel_and_foreign_response_never_mean_consent(self):
        for kind in ('timeout', 'cancel', 'foreign', 'eof'):
            cancel = threading.Event()
            def reader(request):
                if kind == 'timeout': time.sleep(0.05)
                if kind == 'cancel': cancel.set()
                return {'request_id': 'foreign' if kind == 'foreign' else request['id'], 'value': None if kind == 'eof' else 'yes'}
            human = self.cls()(reader, timeout=0.02, cancel=cancel)
            self.assertFalse(human.approve({'action_hash': 'h', 'config_version': 1})['approved'], kind)

    def test_private_input_is_only_in_vault_and_public_events_are_masked(self):
        secret = 'S09b-private-canary-credential'
        human = self.cls()(lambda request: {'request_id': request['id'], 'value': secret})
        ref = human.private_input('test_password')
        self.assertEqual(human.resolve_private(ref), secret)
        self.assertNotIn(secret, str(human.public_events()))
        self.assertNotIn(secret, ref)
        human.close()
        with self.assertRaises(ValueError): human.resolve_private(ref)

    def test_wait_time_is_recorded_separately_from_active_clock(self):
        def reader(request):
            time.sleep(0.03)
            return {'request_id': request['id'], 'value': 'answer'}
        human = self.cls()(reader)
        start = human.active_clock()
        human.ask('q', phase='run')
        self.assertGreater(human.waited_seconds, 0.02)
        self.assertLess(human.active_clock() - start, 0.025)


if __name__ == '__main__': unittest.main()
