"""Порядок host checker feedback и последующих HTTP в разных model steps."""


def feedback_order(run,case):
    want,initial,remaining={'partial_pass':('PASS','createDevice',{'editDevice','readDevice'}),
        'independent_fail':('FAIL','readDevice',{'createDevice'})}[case]
    seen=[];boundary=None;later=[]
    for step in run.get('steps',[]):
        observation=step.get('observation') or {}
        operations=[r['operation_id'] for r in observation.get('responses',[])]
        if boundary is not None:later.extend(operations)
        seen.extend(operations)
        if boundary is None and (step.get('action') or {}).get('name') in {'execute_code','check_goal'}:
            if (observation.get('check') or {}).get('verdict')==want:
                boundary={'step':step['index'],'verdict':want,'http_before_feedback':len(seen),
                    'operations_before_feedback':list(seen)}
    actual=[e['operation_id'] for e in run.get('exchanges',[])]
    consistent=actual==seen
    confirmed=bool(boundary and boundary['operations_before_feedback']==[initial]
        and remaining<=set(later) and consistent)
    count=boundary['http_before_feedback'] if boundary else 0
    return {'confirmed':confirmed,'feedback':boundary,'operations_after_feedback':later,'trace_consistent':consistent,
        'feedback_exchange_ids':[e.get('id') for e in run.get('exchanges',[])[:count]],
        'subsequent_exchange_ids':[e.get('id') for e in run.get('exchanges',[])[count:]]}
