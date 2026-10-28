"""Общие code services: private bodies, approval, auth, metadata и transport."""
from copy import deepcopy

from api_agent.execution.workflow import CatalogWorkflow
from api_agent.execution.session import CatalogSession
from api_agent.policy.config import PolicyStop
from api_agent.execution.state import TestPassword


class CodeWorkflow(CatalogWorkflow):
    def _approve(self,proposal):
        def redact(value):
            if isinstance(value,str):
                for secret in self.secrets():value=value.replace(secret,'[REDACTED]')
                return value
            if isinstance(value,dict):return {k:redact(v) for k,v in value.items()}
            if isinstance(value,list):return [redact(v) for v in value]
            return value
        return self.human.approve(redact(proposal))

    def runtime_manifest(self):
        return {**super().runtime_manifest(),'goal_contract':deepcopy(self._contract),'occurrence_bindings':{}}

    def storage_metadata(self):
        if hasattr(self,'_snapshot_metadata'):return deepcopy(self._snapshot_metadata)
        metadata={'extraction':self._project_extraction,'binding_rules':self._project_bindings,
            'selection_scopes':{},'request_auth':{},'occurrences':[],'admission_rules':{}}
        if self.auth:
            metadata['auth']={k:self.auth[k] for k in ('login_operation','token_path')}
            metadata['auth']['public_body_fields']=list(self.auth.get('public_body_fields',[]))
        return deepcopy(metadata)

    def observation(self,session,exchange,validation):
        result=super().observation(session,exchange,validation)
        from api_agent.execution.auth_feedback import observe_auth
        observe_auth(self, session, exchange, validation)
        self._authenticated=session.state.principal is not None
        return result

    def secrets(self):
        return super().secrets()+self.human.secrets()+tuple(v for body in self.private_bodies.values()
            for v in body.values() if isinstance(v,str) and not isinstance(v,TestPassword))


class CodeSession(CatalogSession):
    def admit(self,args):
        if not {v['id'] for v in self.workflow.accepted.snapshot.get('input_slots',[])}<=self.workflow.inputs.keys():
            raise PolicyStop('clarification_required')
        args=deepcopy(args);name=args['operation_id']
        if self.workflow.request_mode(name,None)=='authenticated' and 'credential_ref' not in args:
            auth=self.workflow.auth
            refs=[v['ref'] for v in self.state.public() if v['secret'] and v['operation_id']==auth['login_operation']
                and v['path']==auth['token_path'] and v.get('auth_epoch')==self.state.auth_epoch
                and v.get('principal')==self.state.principal]
            if len(refs)!=1:raise PolicyStop('auth_reference_denied')
            args['credential_ref']=refs[0]
        private=self.workflow.private_bodies.get(name)
        if private:
            body=args.get('body',{})
            if not isinstance(body,dict):raise PolicyStop('private_body_denied')
            for field in private.keys()&body.keys():
                if not isinstance(private[field],TestPassword) or body[field]!=private[field]:
                    raise PolicyStop('private_body_denied')
            args['body']={**body,**private}
        return super().admit(args)

    def authorize(self,args,result):
        if result[1]['method'] in {'GET','HEAD','OPTIONS'}:
            result=(*result[:-1],(*result[-1],'fresh_read',self.requests))
        waited=self.workflow.human.waited_seconds
        try:return super().authorize(args,result)
        finally:self.deadline+=self.workflow.human.waited_seconds-waited
