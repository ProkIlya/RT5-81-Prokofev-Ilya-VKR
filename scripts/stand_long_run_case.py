"""Условия STAND-33 и отдельный business oracle; не HTTP-план агента."""
from copy import deepcopy


def proposal():
    """Два результата различаются смыслом; будущих ID и шагов здесь нет."""
    first = 'get_api_trips_trip_id'
    first_where = {'parameters':{'trip_id':{'response':{'operation_id':'get_api_trips',
        'path':'/0/id','where':{'parameters':{'status':'сформирован'}}}}}}
    drafts = {'operation_id':first, 'where':{'parameters':{'trip_id':{'response':{
        'operation_id':'get_api_trips_scenarioscart','path':'/trip_id'}}}}}
    return {'checks':[
        {'operation_id':first,'where':deepcopy(first_where),'kind':'equals','path':'/start_charge','expected':57},
        {'operation_id':first,'where':deepcopy(first_where),'kind':'equals','path':'/status','expected':'сформирован'},
        {'operation_id':first,'where':deepcopy(first_where),'kind':'rows','path':'/scenarios','mode':'exact','expected_rows':[
            {'scenario':{'name':'Загородная трасса'},'duration':100},
            {'scenario':{'name':'Кондиционер'},'duration':1}]},
        {**deepcopy(drafts),'kind':'equals','path':'/start_charge','expected':80},
        {**deepcopy(drafts),'kind':'equals','path':'/status','expected':'черновик'},
        {**deepcopy(drafts),'kind':'rows','path':'/scenarios','mode':'exact',
            'expected_rows':[{'scenario':{'name':'Экономичная городская поездка'},'duration':50}]},
        {'operation_id':'get_api_scenarios','kind':'performed'}],
        'answer_fields':[{'operation_id':first,'where':deepcopy(first_where),'path':'/'+key}
            for key in ('id','start_charge','status','scenarios')]}


def independent_result(trips, final_read_id, projection):
    """Состояние читает отдельный наблюдатель, не текст ответа модели."""
    def composition(trip):
        return sorted((r['scenario']['name'],r['duration']) for r in trip['scenarios'])
    first = [t for t in trips if t['start_charge']==57 and t['status']=='сформирован'
        and t.get('submitted_at') and composition(t)==[('Загородная трасса',100),('Кондиционер',1)]]
    second = [t for t in trips if t['start_charge']==80 and t['status']=='черновик'
        and composition(t)==[('Экономичная городская поездка',50)]]
    state_ok = (len(trips)==2 and len(first)==len(second)==1
        and first[0]['id']!=second[0]['id'] and final_read_id==first[0]['id'])
    measured = bool(projection and projection['input_tokens_before']>projection['threshold_tokens']
        and projection['input_tokens_after']<projection['input_tokens_before'])
    return {'two_trip_state':state_ok,'measured_projection':measured,
        'recovery':'passing' if state_ok and measured else 'not_checked' if not measured else 'failed'}
