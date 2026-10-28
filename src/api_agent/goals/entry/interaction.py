"""Application-owned ожидание человека, раздельные ask и приватная память."""
from copy import deepcopy
import queue
import threading
import time
import uuid

from api_agent.bounded_json import validate_tree


class HumanInteraction:
    """Ответ привязан к запросу; EOF/timeout/отмена не означают согласие."""
    def __init__(self, reader=None, *, timeout=120, cancel=None):
        if type(timeout) not in (int, float) or not 0 < timeout <= 86400:
            raise ValueError('invalid_human_timeout')
        self.reader, self.timeout = reader, timeout
        self.cancel = cancel or threading.Event()
        self.counts = {'preparation': 0, 'run': 0}
        self.events, self._private = [], {}
        self._test_passwords = {}
        self.waited_seconds = 0.0

    def active_clock(self):
        return time.perf_counter() - self.waited_seconds

    def _request(self, kind, payload):
        validate_tree(payload, max_bytes=16384)
        request = {'id': str(uuid.uuid4()), 'kind': kind, 'payload': deepcopy(payload), 'timeout_seconds': self.timeout}
        started = time.perf_counter()
        replies = queue.Queue(maxsize=1)
        def collect():
            try: response = self.reader(deepcopy(request)) if self.reader else None
            except Exception: response = None
            replies.put(response)
        threading.Thread(target=collect, daemon=True).start()
        response = None
        try:
            while not self.cancel.is_set() and time.perf_counter() - started < self.timeout:
                try:
                    response = replies.get(timeout=min(0.02, max(0.001, self.timeout - (time.perf_counter() - started))))
                    break
                except queue.Empty: pass
        finally:
            self.waited_seconds += time.perf_counter() - started
        valid = (not self.cancel.is_set() and time.perf_counter() - started < self.timeout
                 and isinstance(response, dict) and set(response) == {'request_id', 'value'}
                 and response['request_id'] == request['id'] and response['value'] is not None)
        self.events.append({'request_id': request['id'], 'kind': kind, 'status': 'answered' if valid else 'unanswered',
                            'wait_seconds': time.perf_counter() - started})
        if not valid:
            raise ValueError('human_cancelled' if self.cancel.is_set() else 'human_unanswered')
        validate_tree(response['value'], max_bytes=8192)
        return response['value']

    def ask(self, question, *, phase):
        if phase not in self.counts or not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValueError('invalid_question')
        if self.counts[phase] >= 3: raise ValueError('ask_limit')
        self.counts[phase] += 1
        return self._request('ask', {'phase': phase, 'question': question})

    def approve(self, proposal):
        result = {'action_hash': proposal['action_hash'], 'config_version': proposal['config_version'], 'approved': False}
        try: result['approved'] = self._request('approval', proposal) == 'yes'
        except ValueError: pass
        return result

    def accept(self, proposal_hash, preview):
        try: approved = self._request('acceptance', {'proposal_hash': proposal_hash, 'interpretation': preview}) == 'yes'
        except ValueError: approved = False
        return {'proposal_hash': proposal_hash, 'approved': approved, 'complete': approved}

    def private_input(self, label, *, phase=None):
        if not isinstance(label, str) or not label or len(label) > 100: raise ValueError('invalid_private_label')
        if phase is not None:
            if phase not in self.counts:raise ValueError('invalid_question')
            if self.counts[phase]>=3:raise ValueError('ask_limit')
            self.counts[phase]+=1
        value = self._request('private_input', {'label': label})
        if not isinstance(value, str) or not value or len(value) > 4096: raise ValueError('invalid_private_value')
        ref = 'private-' + uuid.uuid4().hex
        self._private[ref] = value
        return ref

    def resolve_private(self, ref):
        if ref not in self._private: raise ValueError('unknown_private_input')
        return self._private[ref]

    def secrets(self):
        return tuple(value for value in self._private.values() if value not in self._test_passwords)

    def classify_test_passwords(self, marked):
        self._test_passwords = dict(marked)

    def public_events(self):
        return deepcopy(self.events)

    def close(self):
        self._private.clear()
        self._test_passwords.clear()
