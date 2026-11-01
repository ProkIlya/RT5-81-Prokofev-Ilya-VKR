"""Переносимый диагностический JSON из сохранённого Run без authority-состояния."""
from copy import deepcopy
import json
import re
from api_agent.goals.checks.compiler import PRIVATE_NAMES


def public_copy(value):
    if isinstance(value,dict):
        return {k:('[REDACTED]' if k.lower() in PRIVATE_NAMES or k.lower() in {'authorization','cookie','set-cookie'} else public_copy(v)) for k,v in value.items()}
    if isinstance(value,list):return [public_copy(x) for x in value]
    if isinstance(value,str):return re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s\"\']+','[REDACTED]',value)
    return value


def run_trace(run):
    manifest=((run.get('admission_manifest') or {}).get('goal_contract') or {}).get('manifest',{})
    result={'schema_version':'diagnostic-run-v1',
        **{k:deepcopy(run.get(k)) for k in ('run_id','project_id','session_id','parent_run_id','goal','mode','status','stop_reason','started_at','wall_seconds','uncertain_write','resources')},
        'conditions':[deepcopy(c) for c in manifest.get('cards',[]) if c.get('enabled',True)],
        'attachments':deepcopy(manifest.get('attachments',[])),
        'http':deepcopy(run.get('exchanges',[])),
        'report':deepcopy(run.get('report')),'answer':deepcopy(run.get('final')),
        'model_identity':(run.get('model') or {}).get('snapshot',{}).get('identity'),
        'events':deepcopy(run.get('events',[])),
        'partial_snapshot':not bool(run.get('report'))}
    return json.dumps(public_copy(result),ensure_ascii=False,indent=2)
