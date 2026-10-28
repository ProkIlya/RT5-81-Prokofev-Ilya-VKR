"""Typed missing inputs manual Run; приватный ответ остаётся в приложении."""
from copy import deepcopy
import re

from api_agent.goals.checks.compiler import _closed, PRIVATE_NAMES
from api_agent.policy.config import PolicyStop
from api_agent.validation import schema_issues


def compile_inputs(catalog,config,values):
    if not isinstance(values,list) or len(values)>3:raise ValueError('selected_input_limit')
    result=[];ids=set();bindings=set()
    for value in values:
        _closed(value,{'id','type'},{'label','private','binding'})
        name=value['id'];kind=value['type'];private=value.get('private',False)
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',name) or name in ids:
            raise ValueError('selected_invalid_input_id')
        if kind not in {'string','integer','number','boolean'} or type(private) is not bool:
            raise ValueError('selected_invalid_input_type')
        label=value.get('label',name)
        if not isinstance(label,str) or not 1<=len(label)<=100:raise ValueError('selected_invalid_input_label')
        item={'id':name,'type':kind,'label':label,'private':private}
        if private:
            if kind!='string' or 'binding' not in value:raise ValueError('selected_private_binding_required')
            binding=value['binding'];_closed(binding,{'operation_id','body_field'})
            op,key=binding['operation_id'],binding['body_field']
            if not isinstance(op,str) or op not in config.snapshot['operations'] or not isinstance(key,str):
                raise ValueError('selected_invalid_private_binding')
            body=catalog.operations[op].get('requestBody',{}).get('content',{}).get('application/json',{}).get('schema',{})
            if body.get('properties',{}).get(key,{}).get('type')!='string' or (op,key) in bindings:
                raise ValueError('selected_invalid_private_binding')
            bindings.add((op,key));item['binding']=deepcopy(binding)
        elif 'binding' in value or name.lower() in PRIVATE_NAMES:
            raise ValueError('selected_private_binding_required')
        ids.add(name);result.append(item)
    return result


def ask_input(workflow,session,args):
    if not isinstance(args['question'],str) or not args['question'].strip() or len(args['question'])>2000:
        raise PolicyStop('invalid_question')
    descriptor=next((v for v in workflow.accepted.snapshot['input_slots'] if v['id']==args['input_slot']),None)
    if descriptor is None:raise PolicyStop('goal_revision_required')
    name=descriptor['id']
    if session.requests or name in workflow.inputs:raise PolicyStop('input_slot_locked')
    waited=workflow.human.waited_seconds
    try:
        if descriptor['private']:
            # Worker получает только статус; exact body подставляется общим
            # private-body admission, approvals всё ещё связываются с запросом.
            ref=workflow.human.private_input(descriptor['label'],phase='run')
            value=workflow.human.resolve_private(ref)
            binding=descriptor['binding']
            workflow.private_bodies[binding['operation_id']][binding['body_field']]=value
            workflow.inputs[name]={'private':True}
            answer={'input_slot':name,'private':True,'supplied':True}
        else:
            value=workflow.human.ask(args['question'],phase='run')
            if schema_issues(value,{'type':descriptor['type']}):raise PolicyStop('invalid_human_input')
            workflow.inputs[name]=deepcopy(value)
            answer={'input_slot':name,'value':deepcopy(value)}
    finally:
        session.deadline+=workflow.human.waited_seconds-waited
        session.run['human_events']=workflow.human.public_events()
    session.run['goal_inputs']=deepcopy(workflow.inputs)
    return answer
