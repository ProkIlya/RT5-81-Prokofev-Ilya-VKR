"""Образцы выбранной публичной схемы; не факты API и не принятые ожидания."""
from copy import deepcopy
import re
from api_agent.goals.checks.compiler import PRIVATE_NAMES
from api_agent.validation import schema_issues


def example(schema,depth=0):
    if depth>8:return None
    kind=schema.get('type')
    seed=schema.get('example',schema.get('default'))
    if kind=='array':
        item=deepcopy(schema.get('items',{}))
        if isinstance(seed,list) and seed:item['example']=seed[0]
        return [example(item,depth+1)]
    if kind=='object':
        return {key:example({**value,**({'example':seed[key]} if isinstance(seed,dict) and key in seed else {})},depth+1) for key,value in schema.get('properties',{}).items()
            if key.lower() not in PRIVATE_NAMES and value.get('type')!='array'}
    if seed is not None and not schema_issues(seed,schema):return deepcopy(seed)
    if schema.get('enum'):return deepcopy(schema['enum'][0])
    return {'string':'Пример','integer':1,'number':0.1,'boolean':True,'null':None}.get(kind)


def decimal_text(value):
    text=value.strip().replace(',','.')
    if not re.fullmatch(r'-?\d{1,18}(?:\.\d{1,18})?',text):
        raise ValueError('Введите число с точкой, например 0.10; без разделителей тысяч')
    return text
