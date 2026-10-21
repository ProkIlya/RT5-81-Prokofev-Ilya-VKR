"""Bootstrap внутри контейнера. Его сообщения недоверенны внешнему broker."""
import contextlib
import json
import os
import resource
import sys

# Изолированный Python не добавляет каталог скрипта в sys.path.
sys.path.insert(0, '/runtime')
from api_agent.workspace.sdk import SdkArtifact


class CallDenied(ValueError): pass
class ApprovalRejected(CallDenied): pass
class BudgetExceeded(CallDenied): pass
class Cancelled(CallDenied): pass
class UncertainWrite(CallDenied): pass


class Output:
    """Вывод накапливается ограниченно, а не обрезается после StringIO flood."""
    def __init__(self): self.data = bytearray(); self.truncated = False
    def write(self, text):
        room = max(0, 8192 - len(self.data))
        self.data.extend(text[:room].encode('utf-8')[:room])
        self.truncated |= len(text.encode('utf-8')) > room
        return len(text)
    def flush(self): pass


def main():
    incoming = sys.stdin.buffer
    outgoing = os.fdopen(os.dup(1), 'wb', buffering=0)
    def send(value): outgoing.write(json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b'\n')
    def receive():
        data = incoming.readline(524289)
        if not data or len(data) > 524288: raise EOFError()
        return json.loads(data)
    initial = receive()
    # Hard OS limits нельзя поднять из unprivileged Python даже после monkeypatch SDK.
    resource.setrlimit(resource.RLIMIT_AS, (initial['memory_bytes'], initial['memory_bytes']))
    resource.setrlimit(resource.RLIMIT_CPU, (initial['cpu_seconds'], initial['cpu_seconds']))
    artifact = SdkArtifact.load('/artifact', expected_hash=initial['artifact_hash'])
    execution, sequence = None, 0
    def dispatch(request):
        nonlocal sequence
        sequence += 1
        send({'kind': 'call', 'execution_id': execution, 'sequence': sequence, 'request': request})
        result = receive()
        if result.get('error'):
            error = {'approval_rejected': ApprovalRejected, 'request_limit': BudgetExceeded,
                'cancelled': Cancelled, 'uncertain_write': UncertainWrite}.get(result['error'], CallDenied)
            raise error(result['error'])
        return result['result']
    namespace = {'api': artifact.bind(dispatch), 'ApprovalRejected': ApprovalRejected, 'BudgetExceeded': BudgetExceeded,
        'Cancelled': Cancelled, 'UncertainWrite': UncertainWrite, '__name__': '__workspace__'}
    send({'kind': 'ready', 'artifact_hash': artifact.hash})
    while True:
        command = receive()
        execution, sequence = command['execution_id'], 0
        output, error = Output(), None
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                exec(compile(command['code'], '<workspace>', 'exec'), namespace)
        except BaseException as caught:
            error = type(caught).__name__
        names = [name for name in namespace if isinstance(name, str) and not name.startswith('_') and len(name) <= 80][:32]
        send({'kind': 'done', 'execution_id': execution, 'output': output.data.decode('utf-8', errors='replace'),
            'output_truncated': output.truncated, 'error': error, 'variables': names})


if __name__ == '__main__': main()
