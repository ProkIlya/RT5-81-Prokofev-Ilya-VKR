"""Ограниченная проекция существующих annotations, без изменения API-контракта."""
from .model import canonical, pointer_part, sensitive


def documentation_preview(operation, annotations):
    """Source pointers остаются указателями исходного OpenAPI, не индексами SDK.

    Описания — данные. Они не дополняют schemas, права или ожидаемые результаты.
    Полные сохранённые annotations доступны через существующий SDK describe.
    """
    base = '/paths/' + pointer_part(operation['path'])
    root = base + '/' + operation['method'].lower()
    primary = root + '/description' if root + '/description' in annotations else base + '/description'
    selected = []
    if primary in annotations: selected.append((primary, annotations[primary], True))
    for pointer, annotation in sorted(annotations.items()):
        if pointer == primary or not pointer.endswith('/description'): continue
        if not (pointer.startswith(root + '/') or pointer.startswith(base + '/parameters/')): continue
        # Обычные подписи status responses не дублируем; descriptions полей сохраняем.
        if '/schema/' not in pointer and '/parameters/' not in pointer and not pointer.endswith('/requestBody/description'): continue
        parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer.split('/')]
        if any(sensitive(part) or part.lower() == 'cookie' for part in parts): continue
        selected.append((pointer, annotation, False))
    if not selected: return {}
    result = {'description': '', 'annotations': {}, 'omitted': 0, 'truncated': False}
    for pointer, annotation, is_primary in selected:
        text = annotation.get('text', '')
        if not isinstance(text, str): continue
        preview = text[:512]
        if is_primary: result['description'] = preview
        else: result['annotations'][pointer] = preview
        if len(canonical(result)) > 3072:
            if is_primary: result['description'] = ''
            else: result['annotations'].pop(pointer)
            result['omitted'] += 1
        if len(text) > len(preview) or annotation.get('truncated'): result['truncated'] = True
    result['truncated'] |= result['omitted'] > 0
    if not result['annotations']: result.pop('annotations')
    if not result['omitted']: result.pop('omitted')
    if not result['truncated']: result.pop('truncated')
    return result
