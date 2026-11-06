"""Версионированные пользовательские карточки40; task oracle хранится отдельно."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / 'docs/evals/stand-tasks-v2.plan.ru.json'
LOGIN = 'post_api_users_login'
LOGOUT = 'post_api_users_logout'
PROFILE = 'get_api_users_profile'
REGISTER = 'post_api_users_register'
PASSWORD = 'put_api_users_profile'
LIST = 'get_api_scenarios'
DETAIL = 'get_api_scenarios_id'
CREATE_SCENARIO = 'post_api_scenarios'
EDIT_SCENARIO = 'put_api_scenarios_id'
DELETE_SCENARIO = 'delete_api_scenarios_id'
ADD = 'post_api_scenarios_id_add-to-trip'
TRIPS = 'get_api_trips'
CREATE = 'post_api_trips'
READ = 'get_api_trips_trip_id'
EDIT = 'put_api_trips_trip_id'
DELETE = 'delete_api_trips_trip_id'
CART = 'get_api_trips_scenarioscart'
ROW_EDIT = 'put_api_trips_trip_id_scenarios_scenario_id'
ROW_DELETE = 'delete_api_trips_trip_id_scenarios_scenario_id'
SUBMIT = 'put_api_trips_trip_id_submittrip'
REVIEW = 'put_api_trips_trip_id_reviewtrip'
CITY = 'Экономичная городская поездка'
COUNTRY = 'Загородная трасса'
AC = 'Кондиционер'
COUNTRY_ROWS = [(COUNTRY, 100), (AC, 1)]
CITY_ROWS = [(CITY, 75), (AC, 1.5)]
CATALOG_NAMES = [CITY, AC, 'Зимний город', COUNTRY, 'Зимняя загородная трасса']
EXPECTED_REMAINING_EXACT = 57 - ((2068 * 9.8 * .012 + .5 * 1.225 * .208 * 2.34 * (110 / 3.6) ** 2) * 100000 / 3600000 + 1.5)


def public_energy(value):
    """Публичная точность по согласованному контракту; точная формула отдельно."""
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


EXPECTED_REMAINING = float(public_energy(EXPECTED_REMAINING_EXACT))


def card(key, op, statuses=(200,), *, assertions=(), parameters=None, body=None, selector='last', auth='any'):
    where = {}
    if parameters is not None: where['parameters'] = parameters
    if body is not None: where['body'] = body
    return {'id': key, 'operation_id': op, 'statuses': list(statuses), 'selector': selector,
            'auth': auth, 'where': where, 'assertions': list(assertions)}


def equals(path, value): return {'predicate': 'equals', 'path': path, 'value': value}
def collection(path, rows): return {'predicate': 'collection', 'path': path, 'mode': 'exact', 'rows': rows}


def created(charge=None):
    source = {'operation_id': CREATE, 'path': '/id', 'occurrence': 'last'}
    if charge is not None: source['where'] = {'body': {'start_charge': charge}}
    return {'response': source}


def trip_card(key, *, identifier=None, charge=57, status='черновик', rows=None, creator=None):
    assertions = [equals('/start_charge', charge), equals('/status', status)]
    if creator is not None: assertions.append(equals('/creator_login', creator))
    if rows:
        assertions.append(collection('/scenarios', [{'/scenario/name': name, '/duration': value} for name, value in rows]))
    return card(key, READ, parameters={'trip_id': created() if identifier is None else identifier}, assertions=assertions)


def build_case(number, fixture):
    if type(number) is not int or not 1 <= number <= 40: raise ValueError('stand_case_invalid')
    case = json.loads(PLAN.read_text(encoding='utf-8'))['cases'][number - 1]
    goal = case['goal']; actor = fixture.get('login', 'user'); label = fixture.get('label', 'trial')
    replacements = {'номер_заявки': fixture.get('trip_id', 1), 'номер_чужой_заявки': fixture.get('foreign_id', 1),
        'метка_прогона': label, 'дата_начала': fixture.get('date_start', '2026-10-01'),
        'дата_окончания': fixture.get('date_end', '2026-10-31')}
    for name, value in replacements.items(): goal = goal.replace('{{' + name + '}}', str(value))
    if '{{' in goal: raise ValueError('stand_public_input_missing')
    mode = 'history' if number == 35 else case['mode']
    cards = []; writes = set()
    prepared = fixture.get('trip_id', 1); foreign = fixture.get('foreign_id', 1)
    if number == 5: writes = {ADD}
    if number == 6:
        cards = [card('profile', PROFILE, assertions=[equals('/login', actor), equals('/is_moderator', False)])]
    elif number == 7:
        writes = {REGISTER, CREATE, ADD, EDIT}
        cards = [card('register', REGISTER, (201,), body={'login': actor}), trip_card('trip', rows=COUNTRY_ROWS)]
        goal += '\nПредоставленные тестовые данные: логин ' + actor + ', пароль ' + fixture['password'] + '.'
    elif number == 8:
        writes = {REGISTER}; cards = [card('duplicate', REGISTER, (400,), body={'login': actor})]
        goal += '\nПредоставленный занятый логин: ' + actor + '. Тестовый пароль: ' + fixture['password'] + '.'
    elif number == 9:
        writes = {LOGOUT}
        cards = [card('login_first', LOGIN, selector='first'), card('logout', LOGOUT),
            card('profile_first', PROFILE, selector='first', assertions=[equals('/login', actor)]),
            card('profile_last', PROFILE, assertions=[equals('/login', actor)])]
    elif number == 10:
        writes = {PASSWORD, LOGOUT}
        cards = [card('change', PASSWORD), card('logout', LOGOUT), card('new_login', LOGIN),
                 card('profile', PROFILE, assertions=[equals('/login', actor)])]
        goal += '\nНовый тестовый пароль: ' + fixture['new_password'] + '.'
    elif number == 11:
        writes = {EDIT, ADD, ROW_EDIT, ROW_DELETE}; cards = [trip_card('edited', identifier=prepared, rows=CITY_ROWS)]
    elif number == 12:
        writes = {ROW_DELETE}; cards = [trip_card('removed', identifier=prepared, rows=[(CITY, 75)])]
    elif number == 13:
        writes = {ADD}; cards = [trip_card('deduplicated', identifier=prepared, rows=[(AC, 1.5)])]
    elif number == 14:
        writes = {ROW_DELETE}
        cards = [trip_card('draft', identifier=prepared), card('empty_cart', CART, assertions=[equals('/count', 0)])]
    elif number == 15:
        writes = {CREATE, SUBMIT}; cards = [card('submit_empty', SUBMIT, (400,)), trip_card('empty_draft'),
            card('empty_cart', CART, assertions=[equals('/count', 0)])]
    elif number == 16:
        writes = {CREATE, ADD, EDIT, SUBMIT}
        cards = [card('submitted', SUBMIT), trip_card('trip', status='сформирован', rows=COUNTRY_ROWS)]
    elif number == 17:
        writes = {EDIT, ROW_EDIT}
        cards = [card('reject_charge', EDIT, (403,), parameters={'trip_id': prepared}),
            card('reject_row', ROW_EDIT, (403,), parameters={'trip_id': prepared}),
            trip_card('unchanged', identifier=prepared, status='сформирован', rows=COUNTRY_ROWS)]
    elif number in (18, 33):
        writes = {CREATE, ADD, EDIT, SUBMIT}
        cards = [trip_card('first_trip', identifier=created(57), status='сформирован', rows=COUNTRY_ROWS),
            trip_card('second_trip', identifier=created(80), charge=80, rows=[(CITY, 50)])]
    elif number == 19:
        writes = {REVIEW}
        final = trip_card('completed', identifier=prepared, status='завершён', rows=COUNTRY_ROWS)
        final['assertions'].append({'predicate': 'number_within', 'path': '/remaining_charge',
            'expected': format(EXPECTED_REMAINING, '.2f'), 'tolerance': '0.000001'})
        cards = [card('moderation', REVIEW, parameters={'trip_id': prepared}, body={'action': 'complete'}), final]
    elif number == 20:
        writes = {REVIEW}; cards = [card('reject', REVIEW, parameters={'trip_id': prepared}, body={'action': 'reject'}),
            trip_card('rejected', identifier=prepared, status='отклонён', rows=COUNTRY_ROWS)]
    elif number == 21:
        writes = {REVIEW}; cards = [card('forbidden', REVIEW, (403,), parameters={'trip_id': prepared}, body={'action': 'complete'}),
            trip_card('unchanged', identifier=prepared, status='сформирован', rows=COUNTRY_ROWS)]
    elif number == 22:
        writes = {EDIT}; cards = [card('foreign_read', READ, (403,), parameters={'trip_id': foreign}),
            card('foreign_write', EDIT, (403,), parameters={'trip_id': foreign})]
    elif number == 23:
        writes = {CREATE_SCENARIO, EDIT_SCENARIO}
        source = {'response': {'operation_id': CREATE_SCENARIO, 'path': '/id', 'occurrence': 'last'}}
        cards = [card('created', CREATE_SCENARIO, (201,)), card('edited', EDIT_SCENARIO),
            card('read', DETAIL, parameters={'id': source}, assertions=[equals('/name', 'Обогрев сидений ' + label),
                equals('/type', 'комфорт'), equals('/description', 'Подготовка поездки зимой'), equals('/system_consumption', .6)])]
    elif number in (24, 25):
        writes = {DELETE_SCENARIO} if number == 24 else {CREATE_SCENARIO}
        cards = [card('write', DELETE_SCENARIO if number == 24 else CREATE_SCENARIO,
            (200,) if number == 24 else (403,), parameters={'id': fixture.get('scenario_id', 6)} if number == 24 else None),
            card('catalog', LIST, assertions=[collection('', [{'/name': name} for name in CATALOG_NAMES])])]
    elif number == 26:
        cards = [card('filtered', TRIPS, assertions=[collection('', [
            {'/id': identifier, '/status': 'завершён', '/creator_login': actor}
            for identifier in fixture.get('completed_ids', [prepared])])])]
    elif number in (27, 28):
        writes = {CREATE, ADD} if number == 27 else {DELETE}
        if number == 28: cards = [card('delete', DELETE, parameters={'trip_id': prepared})]
    elif number == 29:
        writes = {CREATE}; cards = [card('create', CREATE, (201,), body={'start_charge': 57})]
    elif number == 30:
        cards = [card('schema', DETAIL, parameters={'id': 4}, assertions=[{'predicate': 'schema'}])]
    elif number == 31:
        writes = {CREATE}; cards = [card('create', CREATE, (201,)), trip_card('read_created')]
    elif number == 34:
        cards = [trip_card('selected', identifier=prepared, rows=CITY_ROWS)]
    elif number == 36:
        writes = {DELETE}; cards = [card('delete', DELETE, parameters={'trip_id': prepared}),
            trip_card('deleted', identifier=prepared, status='удалён'), card('cart', CART, assertions=[equals('/trip_id', 0), equals('/count', 0)])]
    elif number == 37:
        cards = [card('anonymous', PROFILE, (401,), auth='anonymous')]
    elif number in (38, 40):
        writes = {CREATE} | ({ADD, EDIT} if number == 40 else set())
    elif number == 39: writes = {EDIT}
    public = number in {1, 2, 3, 4, 30, 32}
    reads = {LIST, DETAIL} if public else {LIST, DETAIL, TRIPS, READ, CART, PROFILE}
    if number == 37: reads = {PROFILE}
    operations = {name: 'A' for name in reads}
    operations.update({name: 'C' for name in writes})
    if not public and number not in {27, 35, 37}: operations[LOGIN] = 'C'
    if number == 27: operations.update({name: 'DENY' for name in writes})
    if number == 28: operations[DELETE] = 'CONFIRM'
    document = {'schema_version': 'selected-checks-v1', 'goal': goal, 'mode': mode, 'cards': cards}
    if number == 34 and fixture.get('attachment'): document['attachments'] = [deepcopy(fixture['attachment'])]
    return {'case_id': case['id'], 'mode': mode, 'kind': 'history_action' if number == 35 else 'agent_goal',
        'goal': goal, 'document': document, 'operations': operations,
        'policy_mode': 'READ_ONLY' if number == 27 else 'TEST',
        'coverage': 'selected cards + independent state/text/task/safety observer; no full-goal claim by checker',
        'answer_review': ('not applicable: UI history action' if number == 35 else
            'manual semantic review against public goal, frozen state and trace; regex mismatch alone is not a task failure'),
        'capability_notes': ['revoked credential execution requires explicit supported context; absence is not verified'] if number == 9 else
            ['configured login supplies one private body; new-password login must be actually observed'] if number == 10 else
            ['response ID equality to request is assessed by independent task observer'] if number == 31 else []}
