"""Проверка, публикация, expiry и чтение typed Facts из SQLite."""
import json
import time


from .common import _key, _json

class FactHistory:
    def _fact_policy(self, project):
        row = self.db.execute('SELECT * FROM fact_projects WHERE project_key=?', (_key(project),)).fetchone()
        if row is None:
            raise ValueError('fact_unknown_project')
        return dict(row)

    def configure_fact_policy(self, project, *, response_observation, create_read_link):
        """Доверенная настройка Project/среды; модель не меняет TTL."""
        if any(type(v) is not int or not 1 <= v <= 31536000 for v in (response_observation, create_read_link)):
            raise ValueError('fact_invalid_ttl')
        with self.transaction():
            self._fact_policy(project)
            self.db.execute('UPDATE fact_projects SET response_observation=?,create_read_link=? WHERE project_key=?',
                            (response_observation, create_read_link, _key(project)))
            from ..validation import fingerprint
            limits = {'response_observation': response_observation, 'create_read_link': create_read_link}
            for row in self.db.execute('SELECT * FROM project_facts WHERE project_key=?', (_key(project),)).fetchall():
                fact = self._fact_record(row)
                if fact['type'] in limits:
                    fact['expires_at'] = min(fact['expires_at'], fact['last_verified_at'] + limits[fact['type']])
                    self.db.execute('UPDATE project_facts SET payload=?,payload_hash=? WHERE fact_id=?',
                                    (_json(fact), fingerprint(fact), fact['id']))

    def advance_fact_epoch(self, project):
        """Приложение сообщает о настоящем reset; epoch нельзя перемотать назад."""
        with self.transaction():
            self._fact_policy(project)
            self.db.execute('UPDATE fact_projects SET reset_epoch=reset_epoch+1 WHERE project_key=?', (_key(project),))
            return self._fact_policy(project)['reset_epoch']

    def _propose_fact(self, project, session, run_id, descriptor, *, preview=False):
        from ..validation.facts import verify_fact, fact_view
        from ..openapi.parser import Catalog, canonical
        from ..validation import fingerprint
        run = self.read_run(project, session, run_id)
        row = self.db.execute('SELECT lifecycle FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row['lifecycle'] != 'finalized' and not preview:
            raise ValueError('fact_unfinished_run')
        snapshot = self._fact_snapshot(project, session, run_id, run)
        stamp = self.db.execute('SELECT * FROM fact_runs WHERE run_id=?', (run_id,)).fetchone()
        policy = self._fact_policy(project)
        fact = verify_fact(run, snapshot, descriptor, dict(stamp) if stamp else None, policy)
        if preview:
            return {'status': 'pending_finalization', 'type': fact['type'], 'proposal_hash': fingerprint(descriptor)}
        self.db.execute('INSERT OR IGNORE INTO project_facts VALUES(?,?,?,?,?)',
                        (fact['id'], _key(project), run_id, _json(fact), fingerprint(fact)))
        stored = self.db.execute('SELECT * FROM project_facts WHERE fact_id=? AND project_key=?',
                                 (fact['id'], _key(project))).fetchone()
        return fact_view(self._fact_record(stored), Catalog(canonical(run['contract'])), policy, time.time())

    def _fact_record(self, row):
        """Отпечаток ловит повреждение DTO; SQL-связи определяют Project/Run."""
        from ..validation import fingerprint
        fact = json.loads(row['payload'])
        origin = self.db.execute('SELECT payload FROM runs WHERE run_id=? AND project_key=?',
                                 (row['run_id'], row['project_key'])).fetchone()
        saved = json.loads(origin['payload']) if origin else {}
        if (fingerprint(fact) != row['payload_hash'] or fact['id'] != row['fact_id']
                or fact['origin'] != {'project_id': saved.get('project_id'),
                    'session_id': saved.get('session_id'), 'run_id': row['run_id']}):
            raise ValueError('fact_storage_binding_mismatch')
        return fact

    def _fact_snapshot(self, project, session, run_id, run):
        snapshot = self.snapshot(project, session, run_id)
        manifest = run.get('admission_manifest') or {}
        goal = manifest.get('goal_contract')
        if goal:
            from ..validation import fingerprint
            if fingerprint(goal['manifest']) != goal['manifest_hash']: raise ValueError('goal_manifest_changed')
            for name in ('extraction', 'binding_rules'):
                snapshot['metadata'][name] = manifest[name]
        return snapshot

    def propose_fact(self, project, session, run_id, descriptor):
        """Application API принимает только descriptor, никогда supplied evidence/verdict."""
        with self.transaction():
            return self._propose_fact(project, session, run_id, descriptor)

    def preview_fact(self, project, session, run_id, descriptor):
        """Проверить proposal на собственной истории; до finalize факт не публикуется."""
        with self.transaction():
            return self._propose_fact(project, session, run_id, descriptor, preview=True)

    def invalidate_fact(self, project, fact_id, session, run_id, exchange_id):
        """Сохранить опровержение без удаления исходного значения и evidence."""
        from ..validation.facts import contradict_fact
        from ..validation import fingerprint
        with self.transaction():
            row = self.db.execute('SELECT * FROM project_facts WHERE fact_id=? AND project_key=?',
                                  (fact_id, _key(project))).fetchone()
            if row is None:
                raise ValueError('fact_unknown_fact')
            run = self.read_run(project, session, run_id)
            if self.db.execute('SELECT lifecycle FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] != 'finalized':
                raise ValueError('fact_unfinished_run')
            stamp = self.db.execute('SELECT * FROM fact_runs WHERE run_id=?', (run_id,)).fetchone()
            fact = contradict_fact(self._fact_record(row), run, self.snapshot(project, session, run_id),
                                   dict(stamp) if stamp else None, self._fact_policy(project), exchange_id)
            self.db.execute('UPDATE project_facts SET payload=?,payload_hash=? WHERE fact_id=?',
                            (_json(fact), fingerprint(fact), fact_id))
            return fact

    def _collect_facts(self, project, session, run_id):
        from ..validation.facts import automatic_proposals
        from itertools import chain
        run = self.read_run(project, session, run_id)
        if not run.get('contract'):
            return
        snapshot = self._fact_snapshot(project, session, run_id, run)
        for descriptor in chain(automatic_proposals(run, snapshot), run.get('fact_proposals', [])[:16]):
            try:
                self._propose_fact(project, session, run_id, descriptor)
            except ValueError:
                # Неподтверждённое наблюдение не становится фактом. Отказ SQL
                # не подавляется: итог и факты должны фиксироваться атомарно.
                continue

    def read_facts(self, project, *, catalog, operation_ids=None, now=None, include_inactive=False):
        """Без HTTP/LLM: Project facts доступны новой Session с текущими hashes."""
        from ..validation.facts import checked_time, fact_view
        now = checked_time(time.time() if now is None else now)
        if self.db.execute('PRAGMA user_version').fetchone()[0] < 2:
            return []
        if self.db.execute('SELECT 1 FROM fact_projects WHERE project_key=?', (_key(project),)).fetchone() is None:
            return []
        policy = self._fact_policy(project)
        facts = []
        for row in self.db.execute('SELECT * FROM project_facts WHERE project_key=? ORDER BY rowid', (_key(project),)):
            fact = fact_view(self._fact_record(row), catalog, policy, now)
            if operation_ids is None or set(operation_ids) & fact['dependencies'].keys():
                facts.append(fact)
        # Разные подтверждённые значения того же scoped утверждения не
        # разрешаются уверенностью/датой. Оба сохраняются, актуальный выбор закрыт.
        groups = {}
        for fact in facts:
            if fact['type'] == 'response_observation' and fact['status'] == 'VERIFIED':
                key = _json([fact['dependencies'], fact['scope']])
                groups.setdefault(key, []).append(fact)
        for group in groups.values():
            if len({_json(f['value']) for f in group}) > 1:
                for fact in group:
                    fact.update(status='SUPPORTED', freshness='conflict')
        return [f for f in facts if include_inactive or f['status'] == 'VERIFIED']
