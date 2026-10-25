"""Контролируемая подготовка опорного испытания, отдельно от live generator.

Fixture содержит только публичную цель и bindings, не ответные IDs стенда.
Core и workspace не импортируют этот модуль.
"""
from api_agent.validation import fingerprint


def proposal(catalog, config, goal, login):
    slots, conditions = [], []
    def add(name, parameters=None, body=None, dependencies=()):
        key = 's' + str(len(slots) + 1)
        slot = {'id': key, 'operation_id': name, 'parameters': parameters or {},
            'depends_on': list(dict.fromkeys([*dependencies, *([slots[-1]['id']] if slots else [])]))}
        if body is not None: slot['body'] = body
        slots.append(slot)
        conditions.append({'id': 'status_' + key, 'requirement_id': 'r1', 'slot': key,
            'predicate': 'status_in', 'parameters': {'values': [201 if name == 'post_api_users_register' else 200]},
            'source': 'MODEL_PROPOSED', 'basis': None})
        return key
    def ref(slot, path): return {'from_slot': slot, 'path': path}
    add('post_api_users_register', body={'login': login, 'password': {'private': 'test_password'}})
    add('post_api_users_login')
    road = add('get_api_scenarios', {'name': 'Загородная трасса'})
    air = add('get_api_scenarios', {'name': 'Кондиционер'})
    add('post_api_scenarios_id_add-to-trip', {'id': ref(road, '/0/id')}, {}, [road])
    add('post_api_scenarios_id_add-to-trip', {'id': ref(air, '/0/id')}, {}, [air])
    cart = add('get_api_trips_scenarioscart')
    add('put_api_trips_trip_id', {'trip_id': ref(cart, '/trip_id')}, {'start_charge': 57}, [cart])
    add('put_api_trips_trip_id_scenarios_scenario_id',
        {'trip_id': ref(cart, '/trip_id'), 'scenario_id': ref(air, '/0/id')}, {'duration': 1}, [cart, air])
    add('put_api_trips_trip_id_scenarios_scenario_id',
        {'trip_id': ref(cart, '/trip_id'), 'scenario_id': ref(road, '/0/id')}, {'duration': 100}, [cart, road])
    add('put_api_trips_trip_id_submittrip', {'trip_id': ref(cart, '/trip_id')}, dependencies=[cart])
    final = add('get_api_trips_trip_id', {'trip_id': ref(cart, '/trip_id')}, dependencies=[cart])
    for key, value in [('start_charge', 57), ('status', 'сформирован')]:
        conditions.append({'id': key, 'requirement_id': 'r1', 'slot': final,
            'predicate': 'json_value_equals', 'parameters': {'path': '/' + key, 'value': value},
            'source': 'MODEL_PROPOSED', 'basis': None})
    # Ровно две строки, с правильными единицами/длительностями; проверка до finish.
    slots[-1]['scope'] = {'mode': 'response', 'parameters': slots[-1]['parameters']}
    conditions.append({'id': 'composition', 'requirement_id': 'r1', 'slot': final,
        'predicate': 'json_collection_matches', 'parameters': {'path': '/scenarios', 'mode': 'exact', 'rows': [
            {'/scenario/name': 'Кондиционер', '/duration': 1}, {'/scenario/name': 'Загородная трасса', '/duration': 100}]},
        'source': 'MODEL_PROPOSED', 'basis': None})
    return {'version': 1, 'goal_hash': fingerprint(goal), 'spec_hash': catalog.spec_hash,
        'config_hash': fingerprint(config.snapshot), 'requirements': [{'id': 'r1', 'span': [0, len(goal)], 'interpretation': goal}],
        'slots': slots, 'conditions': conditions, 'answer_fields': [
            {'id': key, 'requirement_id': 'r1', 'slot': final, 'path': '/' + key}
            for key in ('id', 'scenarios', 'start_charge', 'status', 'submitted_at')], 'unresolved': []}
