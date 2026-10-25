"""Статическая проверка блока без исполнения Python или пробных HTTP."""
import ast
from difflib import get_close_matches
from os.path import commonprefix

from .sdk import FACADES


def check_code(code, artifact, *, provided_bodies=()):
    """Только доказуемые ошибки; динамические аргументы проверяет настоящий вызов."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError) as error:
        message = getattr(error, 'msg', '')
        hint = 'Fix Python syntax at the indicated line and column.'
        for fragment, advice in (
            ('unterminated string', 'Close the string literal at the indicated line.'),
            ('was never closed', 'Close the bracket or parenthesis at the indicated line.'),
            ('line continuation', 'Use actual newlines, not literal backslash-n outside strings.'),
            ('invalid decimal literal', 'Fix the invalid numeric literal at the indicated line.'),
        ):
            if fragment in message: hint = advice; break
        return {'error': 'SyntaxError', 'reason': 'invalid_python_syntax',
                'line': getattr(error, 'lineno', None), 'column': getattr(error, 'offset', None),
                'hint': hint + ' No code was executed; resubmit the corrected block.'}
    operations = {op['python_name']: (name, op) for name, op in artifact.manifest['operations'].items()}
    response_diagnostic = _response_diagnostic(tree, operations)
    diagnostics = [response_diagnostic] if response_diagnostic else []
    for node in sorted(ast.walk(tree), key=lambda item: getattr(item, 'lineno', 0)):
        diagnostic = _call_diagnostic(node, operations, provided_bodies)
        if diagnostic:
            diagnostics.append(diagnostic)
            if len(diagnostics) == 9: break
    if not diagnostics: return None
    diagnostics.sort(key=lambda item: item.get('line', 0))
    result = dict(diagnostics[0])
    if len(diagnostics) > 1:
        result['diagnostics'] = diagnostics[:8]
        result['diagnostics_truncated'] = len(diagnostics) > 8
        result['hint'] = 'Correct all reported calls. No code was executed. ' + ' '.join(
            'Line ' + str(d['line']) + ': ' + d.get('function', ', '.join(d.get('candidates', [])))
            + '; ' + d['reason'] + '; allowed kwargs: ' + ', '.join(d.get('allowed_arguments', []))
            + '; required: ' + ', '.join(d.get('required_arguments', [])) + '.' for d in diagnostics[:8])
        if response_diagnostic: result['hint'] += ' ' + response_diagnostic['hint']
    return result


def _response_diagnostic(tree, operations):
    """Прямой SDK result — dict; не угадывать тип после ветвлений/чужих calls."""
    known = {}; sdk_ready = True
    for statement in tree.body:
        writes = {n.id for n in ast.walk(statement) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        if 'api' in writes: sdk_ready = False; known.clear()
        if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)):
            known.clear(); continue
        def safe_call(node):
            function = node.func
            return (isinstance(function, ast.Name) and function.id == 'print' and 'print' not in writes
                or isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                and (function.value.id == 'api' or function.value.id in known))
        if any(isinstance(n, (ast.NamedExpr, ast.Await, ast.Yield))
            or isinstance(n, ast.Call) and not safe_call(n) for n in ast.walk(statement)):
            known.clear()
        for node in ast.walk(statement):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in known and node.attr in {'status_code', 'json', 'body', 'status'}):
                name, operation = known[node.value.id]
                return {'error':'AttributeError','reason':'sdk_response_is_mapping','line':node.lineno,
                    'operation_id':name,'function':'api.' + operation['python_name'],
                    'hint':'SDK results are dicts. Use result["status"] and result["body"], not .status_code or .json(). '
                        'Correct all response access before resubmitting. No code was executed.'}
        for name in writes: known.pop(name, None)
        value = getattr(statement, 'value', None)
        targets = statement.targets if isinstance(statement, ast.Assign) else [getattr(statement, 'target', None)]
        if (sdk_ready and isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and isinstance(value.func.value, ast.Name) and value.func.value.id == 'api'
            and value.func.attr in operations and all(isinstance(t, ast.Name) for t in targets)):
            for target in targets: known[target.id] = operations[value.func.attr]
    return None


def _call_diagnostic(node, operations, provided_bodies):
    """Форма одного прямого вызова, без отражения выражений и значений аргументов."""
    if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
            or not isinstance(node.func.value, ast.Name) or node.func.value.id != 'api'):
        return None
    function = node.func.attr
    if function in FACADES or function.startswith('_'):
        return None
    if function not in operations:
        if function in {'execute_code', 'check_goal', 'finish_run'}:
            hint = function + ' is an outer tool, not an api function. Call it as a tool outside Python.'
            candidates = []
        else:
            candidates = get_close_matches(function, sorted(operations), n=len(operations), cutoff=0.45)
            supplied = {k.arg for k in node.keywords if k.arg is not None}
            compatible = []
            for candidate in candidates:
                name, operation = operations[candidate]
                fields = operation['parameter_mapping'] + ([{'python_name': 'body',
                    'required': operation['body_required'] and name not in provided_bodies}]
                    if operation['body'] is not None else [])
                if (not node.args and not any(k.arg is None for k in node.keywords)
                    and {p['python_name'] for p in fields if p['required']} <= supplied
                    and supplied <= {p['python_name'] for p in fields}):
                    compatible.append(candidate)
            candidates = sorted(compatible or candidates,
                key=lambda candidate: len(commonprefix([function, candidate])), reverse=True)[:3]
            hint = ('Closest available function accepting these argument names: api.' + candidates[0]
                + '. Copy this exact name if it is the required operation; otherwise choose from operations.'
                if compatible else 'Copy the exact api function name and required arguments from operations.')
        return {'error': 'AttributeError', 'reason': 'unknown_api_function', 'line': node.lineno,
                'candidates': ['api.' + item for item in candidates],
                'hint': hint + ' No code was executed; correct the rejected block.'}
    name, operation = operations[function]
    fields = operation['parameter_mapping'] + ([{'python_name': 'body',
        'required': operation['body_required'] and name not in provided_bodies}]
        if operation['body'] is not None else [])
    allowed = {p['python_name'] for p in fields}
    required = {p['python_name'] for p in fields if p['required']}
    supplied = {k.arg for k in node.keywords if k.arg is not None}
    diagnostic = {'error': 'TypeError', 'operation_id': name, 'line': node.lineno,
        'function': 'api.' + function, 'allowed_arguments': sorted(allowed),
        'required_arguments': sorted(required)}
    if supplied - allowed:
        return {**diagnostic, 'reason': 'unexpected_keyword_argument',
                'hint': 'For api.' + function + ' use only these keyword arguments: '
                + ', '.join(sorted(allowed)) + '. Required: ' + ', '.join(sorted(required))
                + '. Replace the incorrect argument names. No code was executed; correct the block.'}
    if any(not isinstance(arg, ast.Starred) for arg in node.args):
        return {**diagnostic, 'reason': 'keyword_only_arguments',
                'hint': 'Use named keyword arguments, not positional arguments. No code was executed.'}
    if not node.args and not any(k.arg is None for k in node.keywords) and required - supplied:
        return {**diagnostic, 'reason': 'missing_required_argument',
                'hint': 'Supply the required keyword arguments. No code was executed; correct the block.'}
    body = next((k.value for k in node.keywords if k.arg == 'body'), None)
    if (name in provided_bodies and body is not None
            and isinstance(body, (ast.Dict, ast.List, ast.Constant))
            and not (isinstance(body, ast.Constant) and body.value is None)):
        return {**diagnostic, 'reason': 'application_supplied_body', 'call': 'api.' + function + '()',
                'hint': 'Replace this call with api.' + function + '() without body. No code was executed; resubmit the corrected block.'}
    return None
