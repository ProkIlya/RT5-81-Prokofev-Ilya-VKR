"""Ограниченная read model и атомарная запись versioned отчёта в SQLite."""
from copy import deepcopy
import json

from api_agent.storage.common import _key, _json, StorageError
from api_agent.validation import fingerprint, report
from api_agent.agent.response_views import response_page


def page_bounds(limit,offset):
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:
        raise ValueError('history_invalid_page')


def page(items,limit,offset):
    return {'items':items[:limit],'offset':offset,'limit':limit,'more':len(items)>limit}


class ResultHistory:
    def _save_report(self,row,run):
        saved=run.get('report') or {}
        if saved.get('schema_version')!='run-report-v1':return
        if saved!=report(run):raise ValueError('report_content_mismatch')
        encoded=_json(saved)
        old=self.db.execute('SELECT * FROM reports WHERE report_id=? OR run_id=?',(saved['report_id'],run['run_id'])).fetchone()
        if old:
            if old['payload']!=encoded or old['run_id']!=run['run_id']:raise ValueError('report_id_conflict')
            return
        self.db.execute('INSERT INTO reports VALUES(?,?,?,?,?,?)',
            (saved['report_id'],run['run_id'],row['project_key'],row['session_key'],fingerprint(saved),encoded))

    def read_report(self,project,session,run_id):
        """Читать сохранённую версию, не переоценивать её новым checker."""
        run=self.read_run(project,session,run_id)
        saved=run.get('report')
        if saved is None:raise ValueError('report_not_finalized')
        return deepcopy(saved)

    def _validate_report_snapshot(self,project,session,run):
        """Единый guard публичных DTO; checker здесь никогда не вызывается."""
        saved=run.get('report') or {}
        run_id=run['run_id']
        selected=((run.get('admission_manifest') or {}).get('goal_contract') or {}).get('manifest',{}).get('contract')=='selected-checks-v1'
        report_store=self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'").fetchone() is not None
        if self.db.execute('PRAGMA user_version').fetchone()[0]<4:
            # S10-маркеры или его отдельное хранилище несовместимы с legacy schema.
            if selected or saved.get('schema_version')=='run-report-v1' or report_store:
                raise ValueError('report_schema_incompatible')
            return
        if not report_store:raise ValueError('report_schema_incompatible')
        row=self.db.execute('SELECT * FROM reports WHERE run_id=? AND project_key=? AND session_key=?',
            (run_id,*self._scope(project,session))).fetchone()
        if row is None and not selected and saved.get('schema_version')!='run-report-v1':return
        if row is None:raise ValueError('report_not_saved')
        value=json.loads(row['payload'])
        if fingerprint(value)!=row['payload_hash'] or value!=saved or value.get('report_id')!=row['report_id']:
            raise ValueError('report_snapshot_changed')
        if (value.get('run_id'),value.get('project_id'),value.get('session_id'))!=(run_id,project,session):
            raise ValueError('report_scope_mismatch')
        events=run.get('exchanges',[])
        if any(e.get('run_id')!=run_id for e in events) or {e['id']:fingerprint(e) for e in events}!=value['evidence_hashes']:
            raise ValueError('report_evidence_changed')
        if value['evidence_ids']!=[e['id'] for e in events]:raise ValueError('report_evidence_changed')

    def list_projects(self,*,limit=20,offset=0):
        page_bounds(limit,offset)
        rows=self.db.execute("SELECT p.*, (SELECT json_extract(r.payload,'$.project_id') FROM runs r WHERE r.project_key=p.project_key LIMIT 1) public_id FROM projects p ORDER BY p.rowid LIMIT ? OFFSET ?",(limit+1,offset)).fetchall()
        return page([{'project_id':r['public_id'],'target':r['target'],'environment':r['environment']} for r in rows],limit,offset)

    def list_sessions(self,project,*,limit=20,offset=0):
        page_bounds(limit,offset)
        rows=self.db.execute('SELECT public_id FROM sessions WHERE project_key=? ORDER BY rowid LIMIT ? OFFSET ?',(_key(project),limit+1,offset)).fetchall()
        return page([{'session_id':r['public_id']} for r in rows],limit,offset)

    def list_messages(self,project,session,*,limit=20,offset=0):
        page_bounds(limit,offset)
        rows=self.db.execute('SELECT public_id,role,content,run_id FROM messages WHERE project_key=? AND session_key=? ORDER BY rowid LIMIT ? OFFSET ?',(*self._scope(project,session),limit+1,offset)).fetchall()
        result=[]
        for row in rows:
            text=row['content'];result.append({'message_id':row['public_id'],'role':row['role'],
                'content':text[:4000],'content_truncated':len(text)>4000,'run_id':row['run_id']})
        return page(result,limit,offset)

    def report_page(self,project,session,run_id,*,limit=20,offset=0,section='conditions'):
        page_bounds(limit,offset)
        if section not in {'conditions','contract_diagnostics'}:raise ValueError('history_invalid_report_section')
        saved=self.read_report(project,session,run_id)
        rows=saved.get(section,[])
        metadata={k:deepcopy(v) for k,v in saved.items() if k not in {'conditions','goal_checklist','contract_diagnostics','evidence_hashes'}}
        return {'report':metadata,'section':section,'total':len(rows),**page(deepcopy(rows[offset:offset+limit+1]),limit,offset)}

    def exchanges_page(self,project,session,run_id,*,limit=20,offset=0):
        page_bounds(limit,offset)
        run=self.read_run(project,session,run_id)
        if run.get('report'):self.read_report(project,session,run_id)
        events=run.get('exchanges',[])[offset:offset+limit+1]
        result=[{'exchange_id':e['id'],'sequence':offset+i+1,'operation_id':e['operation_id'],
            'request':{k:(e.get('request') or {}).get(k) for k in ('method','url')},
            'status':(e.get('response') or {}).get('status'),'outcome':e['outcome'],
            'truncated':bool(e.get('truncated')),'wall_seconds':e.get('wall_seconds'),
            'effect_status':e.get('effect_status'),'transport_error':e.get('transport_error')} for i,e in enumerate(events)]
        return page(result,limit,offset)

    def exchange_section(self,project,session,run_id,exchange_id,*,section='response',pointer='',offset=0,limit=16):
        page_bounds(limit,offset)
        if section not in {'request','response'} or limit>16:raise ValueError('history_invalid_section')
        run=self.read_run(project,session,run_id)
        if run.get('report'):self.read_report(project,session,run_id)
        event=next((e for e in run.get('exchanges',[]) if e['id']==exchange_id and e.get('run_id')==run_id),None)
        if event is None:raise ValueError('history_foreign_exchange')
        return {'exchange_id':exchange_id,'section':section,'pointer':pointer,
            'source_truncated':bool(event.get('truncated')),**response_page(event.get(section),json_pointer=pointer,offset=offset,limit=limit,max_bytes=8192)}
