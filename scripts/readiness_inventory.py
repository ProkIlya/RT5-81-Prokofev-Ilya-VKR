"""Аудит карты сравнения: объявленная capability не доказывает готовность сценария."""
import hashlib
import json
from pathlib import Path
from api_agent.validation import fingerprint

DEFERRED = {'STAND-34': 'S11', 'STAND-35': 'S10'}
PROOFS = ('conditions', 'fixture', 'code', 'catalog', 'oracle')
CONSTRUCTOR = {'goal_required': True, 'minimum_total_conditions': 1,
    'required_actions_section_required': False, 'expected_result_section_required': False,
    'sequence_is_agent_choice': True, 'recovered_intermediate_http_error_is_automatic_failure': False,
    'future_entity_ids_from_oracle': False}


def require(condition, code):
    if not condition: raise ValueError(code)


def proof_verified(proof):
    require(proof.get('state') in ('not_checked', 'missing', 'passing', 'failing'), 'proof_state')
    if proof['state'] != 'passing': return False
    require(isinstance(proof.get('evidence'), str) and isinstance(proof.get('sha256'), str), 'proof_missing')
    path = Path(proof['evidence']).resolve()
    try: path.relative_to(Path('evidence').resolve())
    except ValueError: raise ValueError('proof_outside_evidence') from None
    require(path.is_file(), 'proof_file_missing')
    require(hashlib.sha256(path.read_bytes()).hexdigest() == proof['sha256'], 'proof_changed')
    return True


def validate_inventory(suite, inventory, *, require_ready=False, source_file=None):
    require(inventory['source_suite_fingerprint'] == fingerprint(suite), 'suite_changed')
    if source_file is not None:
        source = Path(source_file)
        require(Path(inventory['source_suite']) == source, 'different_source_suite')
        require(inventory['source_suite_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest(), 'suite_changed')
        require(json.loads(source.read_text(encoding='utf-8')) == suite, 'different_source_content')
    require(inventory['version'] == 'owner-2026-10-23-memory-deferred', 'scope_version')
    require(inventory['constructor_contract'] == CONSTRUCTOR, 'constructor_contract')
    require(inventory['assessment']['version'] == 'human-conditions-final-state-v1', 'assessment_version')
    require(inventory['assessment']['state'] in ('planned_not_implemented', 'implemented'), 'assessment_state')
    originals = {case['id']: case for case in suite['cases']}
    require(set(DEFERRED).issubset(originals), 'deferred_cases_missing')
    cases = inventory['cases']; ids = [c['id'] for c in cases]
    require(len(originals) == suite['case_count'] and len(ids) == len(set(ids)), 'duplicate_case')
    require(ids == [c['id'] for c in suite['cases']], 'case_inventory_changed')
    capabilities = {cap for case in suite['cases'] for cap in case['required_capabilities']}
    require(set(inventory['capabilities']) == capabilities, 'capability_inventory_changed')
    for capability in inventory['capabilities'].values():
        for name in ('implementation', 'technical_test'):
            if capability[name]: require(Path(capability[name]).is_file(), 'capability_path_missing')
    selected = ready = 0
    for case in cases:
        original = originals[case['id']]
        require(case['source_goal_sha256'] == hashlib.sha256(original['goal'].encode()).hexdigest(), 'goal_changed')
        require(case['required_capabilities'] == original['required_capabilities'], 'case_capabilities_changed')
        require(case['variants'] == original.get('variants', ['good']), 'variants_changed')
        deferred = case['id'] in DEFERRED
        require(case['comparison_scope'] == ('deferred' if deferred else 'selected'), 'scope_changed')
        if deferred: require(case.get('deferred', {}).get('stage') == DEFERRED[case['id']], 'deferred_stage_changed')
        require(set(case['readiness']) == set(PROOFS), 'readiness_proofs_missing')
        verified = [proof_verified(case['readiness'][name]) for name in PROOFS]
        actual_ready = not deferred and all(verified)
        require(type(case['ready_for_series']) is bool and case['ready_for_series'] == actual_ready, 'readiness_unproven')
        selected += not deferred; ready += actual_ready
    require(inventory['selected'] == selected and inventory['deferred'] == len(DEFERRED), 'scope_counts')
    require(inventory['minimum_base_attempts'] == selected * 3 * 2, 'comparison_budget_changed')
    if require_ready:
        require(ready == selected, 'comparison_not_ready')
        require(inventory['assessment'].get('state') == 'implemented', 'assessment_not_implemented')
    return {'cases': len(cases), 'selected': selected, 'deferred': len(DEFERRED), 'ready': ready}
