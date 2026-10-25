"""Bootstrap внутри контейнера. Его сообщения недоверенны внешнему broker."""
import contextlib
import io
import json
import os
import resource
import sys
import types

# Изолированный Python не добавляет каталог скрипта в sys.path.
sys.path.insert(0, '/runtime')
from api_agent.workspace.sdk import SdkArtifact
from api_agent.workspace.diagnostics import public_keys, python_detail


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
    protocol_input = sys.stdin
    incoming = protocol_input.buffer
    # Python input() не должен ждать или поглощать служебные сообщения broker.
    sys.stdin = sys.__stdin__ = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
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
    diagnostic_keys = public_keys(artifact)
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
    facade = artifact.bind(dispatch)
    # Полностью предоставленное приложением тело не требует аргумента модели.
    for operation_id in initial.get('provided_bodies', []):
        function = getattr(facade, artifact.manifest['operations'][operation_id]['python_name'])
        function.__kwdefaults__ = {**(function.__kwdefaults__ or {}), 'body': None}
    errors = {kind.__name__: kind for kind in (CallDenied, ApprovalRejected, BudgetExceeded, Cancelled, UncertainWrite)}
    # Обе обычные формы import ссылаются на тот же facade и тот же внешний broker.
    module = types.ModuleType('api')
    module.api = facade
    module.__dict__.update(errors)
    module.__getattr__ = lambda name: getattr(facade, name)
    sys.modules['api'] = module
    namespace = {'api': facade, **errors, '__name__': '__workspace__'}
    send({'kind': 'ready', 'artifact_hash': artifact.hash})
    while True:
        command = receive()
        execution, sequence = command['execution_id'], 0
        output, error, error_line, error_object_type, error_detail = Output(), None, None, None, None
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                exec(compile(command['code'], '<workspace>', 'exec'), namespace)
        except BaseException as caught:
            error = type(caught).__name__
            error_detail = python_detail(caught, diagnostic_keys)
            trace = caught.__traceback__
            while trace is not None:
                if trace.tb_frame.f_code.co_filename == '<workspace>': error_line = trace.tb_lineno
                trace = trace.tb_next
            if error_line is None and isinstance(caught, SyntaxError): error_line = caught.lineno
            if isinstance(caught, AttributeError):
                kind = type(getattr(caught, 'obj', None)).__name__
                if kind in {'dict', 'list', 'str', 'int', 'float', 'bool'}: error_object_type = kind
        names = [name for name in namespace if isinstance(name, str) and not name.startswith('_') and len(name) <= 80][:32]
        # Только закрытые названия builtin types, без repr/getattr пользовательских объектов.
        kinds = {dict: 'dict', list: 'list', str: 'str', int: 'int', float: 'float', bool: 'bool',
            tuple: 'tuple', set: 'set', type(None): 'NoneType'}
        variable_types = {name: next((kind for cls, kind in kinds.items()
            if type(namespace[name]) is cls), 'other') for name in names}
        send({'kind': 'done', 'execution_id': execution, 'output': output.data.decode('utf-8', errors='replace'),
            'output_truncated': output.truncated, 'error': error, 'error_line': error_line,
            'error_object_type': error_object_type, 'error_detail': error_detail, 'variables': names,
            'variable_types': variable_types})


if __name__ == '__main__': main()
