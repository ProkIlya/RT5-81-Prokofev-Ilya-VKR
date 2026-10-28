"""Project bookmarks сохранённых S10 отчётов, независимо от Sessions."""
import time
from api_agent.storage.common import _key
from api_agent.storage.results import page_bounds, page


class PinHistory:
    def pin_report(self,project,session,run_id,*,label=''):
        if not isinstance(label,str) or len(label)>200:raise ValueError('pin_invalid_label')
        with self.transaction():
            report=self.read_report(project,session,run_id)
            if report.get('schema_version')!='run-report-v1':raise ValueError('pin_unsupported_report')
            self.db.execute('INSERT INTO report_pins VALUES(?,?,?,?) ON CONFLICT(project_key,report_id) DO UPDATE SET label=excluded.label',
                (_key(project),report['report_id'],label,time.time()))
            row=self.db.execute('SELECT * FROM report_pins WHERE project_key=? AND report_id=?',(_key(project),report['report_id'])).fetchone()
            return {'report_id':report['report_id'],'run_id':run_id,'label':row['label'],'created_at':row['created_at']}

    def unpin_report(self,project,session,run_id):
        with self.transaction():
            report=self.read_report(project,session,run_id)
            self.db.execute('DELETE FROM report_pins WHERE project_key=? AND report_id=?',(_key(project),report.get('report_id')))

    def pinned_reports(self,project,*,limit=20,offset=0):
        page_bounds(limit,offset)
        if self.db.execute('PRAGMA user_version').fetchone()[0]<5:return page([],limit,offset)
        rows=self.db.execute('SELECT p.*,r.run_id,s.public_id session_id FROM report_pins p JOIN reports r USING(report_id) JOIN sessions s ON s.session_key=r.session_key WHERE p.project_key=? AND r.project_key=p.project_key ORDER BY p.created_at,p.report_id LIMIT ? OFFSET ?',(_key(project),limit+1,offset)).fetchall()
        result=[]
        for row in rows:
            report=self.read_report(project,row['session_id'],row['run_id'])
            result.append({'report_id':row['report_id'],'run_id':row['run_id'],'session_id':row['session_id'],
                'label':row['label'],'created_at':row['created_at'],'checks_verdict':report['checks_verdict'],'summary':report['summary']})
        return page(result,limit,offset)
