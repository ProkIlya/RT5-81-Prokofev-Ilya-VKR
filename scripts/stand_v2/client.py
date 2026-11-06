"""Owner/reset/SQL observer отдельного vkr-s14-v2; ключ не часть agent API."""
import http.client
import json
import os
from pathlib import Path
import subprocess

from api_agent.workspace.execution.sandbox import DOCKER, DOCKER_HOST

ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = ROOT / 'experiments/s14-stand-v2'
TARGET = 'http://127.0.0.1:18282'
SPEC = ENVIRONMENT / 'backend/docs/openapi.json'


class PreparationError(ValueError):
    """Недостоверная подготовка не превращается в model FAIL."""


class Owner:
    def __init__(self, *, api_port=18282, control_port=18286, key=None):
        if api_port == control_port or any(type(p) is not int or not 1 <= p <= 65535 for p in (api_port, control_port)):
            raise PreparationError('stand_ports_invalid')
        self.api_port, self.control_port = api_port, control_port
        if key is None:
            try:
                lines = (ENVIRONMENT / 'control.env').read_text(encoding='utf-8').splitlines()
                key = next(line.split('=', 1)[1] for line in lines if line.startswith('STAND_CONTROL_KEY='))
            except (OSError, StopIteration): raise PreparationError('stand_owner_configuration_missing') from None
        if not isinstance(key, str) or len(key) < 32: raise PreparationError('stand_owner_configuration_missing')
        self._key = key
        self.db = 'vkr-s14-v2-postgres-1'

    def _request(self, method, path, *, body=None, token=None, control=False, timeout=10):
        port = self.control_port if control else self.api_port
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        headers = {'Accept': 'application/json'}
        if control: headers['X-Stand-Control-Key'] = self._key
        if token and not control: headers['Authorization'] = 'Bearer ' + token
        if body is not None: headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
            response = connection.getresponse(); raw = response.read(262145)
            if len(raw) > 262144: raise PreparationError('stand_response_limit')
            return response.status, json.loads(raw) if raw else None
        finally: connection.close()

    def request(self, method, path, *, body=None, token=None, expected=200, timeout=10):
        status, value = self._request(method, path, body=body, token=token, timeout=timeout)
        if status != expected: raise PreparationError('stand_fixture_http_status_' + str(status))
        return value

    def control(self, method, path, body=None):
        status, value = self._request(method, path, body=body, control=True,
                                      timeout=90 if method == 'POST' and path == '/reset' else 10)
        if status != 200:
            error = PreparationError('stand_control_failed')
            error.diagnostic = {'http_status': status,
                'phase': value.get('phase') if isinstance(value, dict) and value.get('phase') in {'sql', 'redis'} else 'unknown',
                'code': value.get('error') if isinstance(value, dict) and value.get('error') in {
                    'reset_failed', 'reset_verification_failed', 'entropy_unavailable'} else 'unclassified'}
            raise error
        return value

    def reset(self):
        state = self.control('POST', '/reset', {'confirm': 'RESET_TEST_DATA'})
        if not isinstance(state, dict) or not state.get('ready') or state.get('mode') != 'good' or state.get('counts') != {
                'users': 3, 'scenarios': 5, 'trips': 1, 'trip_scenarios': 2}:
            raise PreparationError('stand_reset_postcondition_failed')
        return state

    def mode(self, mode): return self.control('POST', '/mode', {'mode': mode})
    def state(self): return self.control('GET', '/state')

    def images(self):
        docker = [DOCKER, '--config', str(ROOT / 'evidence/S14/docker-client'), '--host', DOCKER_HOST]
        result = {}
        for service in ('backend-http', 'postgres', 'redis', 'minio', 'async-server'):
            name = 'vkr-s14-v2-' + service + '-1'
            check = subprocess.run(docker + ['inspect', '--format',
                '{{.Image}} {{index .Config.Labels "com.docker.compose.project"}}', name], capture_output=True, timeout=10)
            parts = check.stdout.decode().strip().split()
            if check.returncode or len(parts) != 2 or parts[1] != 'vkr-s14-v2':
                raise PreparationError('stand_image_identity_unproven')
            result[service] = parts[0]
        return result

    def sql(self, query):
        docker = [DOCKER, '--config', str(ROOT / 'evidence/S14/docker-client'), '--host', DOCKER_HOST]
        check = subprocess.run(docker + ['inspect', '--format', '{{index .Config.Labels "com.docker.compose.project"}}', self.db],
                               capture_output=True, timeout=10)
        if check.returncode or check.stdout.strip() != b'vkr-s14-v2': raise PreparationError('stand_database_scope_mismatch')
        result = subprocess.run(docker + ['exec', '-i', self.db, 'sh', '-c',
            'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1'],
            input=query.encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        if result.returncode: raise PreparationError('stand_sql_failed')
        return result.stdout.decode('utf-8').strip()

    def snapshot(self):
        query = """SELECT json_build_object(
          'users',(SELECT json_agg(json_build_object('id',id,'login',login,'moderator',is_moderator) ORDER BY id) FROM users),
          'scenarios',(SELECT json_agg(json_build_object('id',id,'name',name,'status',status,'type',type,
            'description',description,'power',system_consuption,'speed',speed,'aero',aero_coeff,'rolling',rolling_coeff) ORDER BY id) FROM driving_scenarios),
          'trips',(SELECT COALESCE(json_agg(json_build_object('id',t.id,'creator',t.creator_id,'status',t.status,
            'charge',t.start_charge,'remaining',t.remaining_charge,'submitted',t.submitted_at,'created',t.created_at,
            'rows',COALESCE((SELECT json_agg(json_build_object('scenario_id',s.driving_scenario_id,'name',d.name,'duration',s.duration)
                ORDER BY s.driving_scenario_id) FROM trip_scenarios s JOIN driving_scenarios d ON d.id=s.driving_scenario_id
                WHERE s.trip_application_id=t.id),'[]')) ORDER BY t.id),'[]') FROM trip_applications t));"""
        result = json.loads(self.sql(query))
        if not all(isinstance(result.get(key), list) for key in ('users', 'scenarios', 'trips')):
            raise PreparationError('stand_snapshot_incomplete')
        return result

    def save(self, path, value):
        raw = json.dumps(value, ensure_ascii=False, indent=2)
        if self._key in raw: raise PreparationError('stand_owner_key_leak')
        Path(path).write_text(raw + '\n', encoding='utf-8')
