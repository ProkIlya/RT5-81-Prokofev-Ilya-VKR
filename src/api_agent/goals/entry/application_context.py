"""Общий application context/private store без генератора и legacy loop."""
from copy import deepcopy
import threading

from api_agent.execution.contracts import CatalogLimits
from api_agent.goals.entry.interaction import HumanInteraction
from api_agent.goals.entry.test_passwords import TestPassword, authorize


class ApplicationContext:
    def __init__(self,catalog,config,*,history,workspace,human=None,limits=None,mappings=None,
                 token_counter=None,current_config=None,trace_sink=None,event_sink=None,model_test_passwords=None):
        if config.snapshot['spec_hash']!=catalog.spec_hash:raise ValueError('goal_config_scope_mismatch')
        self.target,self.catalog,self.config,self.history=config.snapshot['target'],catalog,config,history
        self.human,self.limits=human or HumanInteraction(),limits or CatalogLimits()
        self.mappings,self.current_config=deepcopy(mappings or {}),current_config
        self.token_counter=token_counter
        self.trace_sink,self.event_sink=trace_sink,event_sink
        self.model_test_passwords=deepcopy(model_test_passwords or {})
        self._test_passwords=authorize(config,self.mappings,self.model_test_passwords) if self.model_test_passwords else {}
        if self._test_passwords:self.human.classify_test_passwords(self._test_passwords)
        self.workspace=workspace;workspace.validate(catalog,config);self.model_name=workspace.model_name
        self._lock=threading.Lock()

    def _safe(self,value):
        secrets=self.human.secrets()+tuple(v for k,v in self.mappings.get('auth',{}).get('body',{}).items()
            if isinstance(v,str) and k not in self.mappings.get('auth',{}).get('public_body_fields',[]))
        secrets+=tuple(v for body in self.mappings.get('private_bodies',{}).values() for v in body.values() if isinstance(v,str))
        if isinstance(value,str):
            for secret in sorted(set(secrets),key=len,reverse=True):
                if secret and not isinstance(secret,TestPassword):value=value.replace(secret,'[REDACTED]')
            return value
        if isinstance(value,dict):return {self._safe(k):self._safe(v) for k,v in value.items()}
        if isinstance(value,list):return [self._safe(v) for v in value]
        return value

    def _save(self,project,session,message,preparation):
        self.history.save_goal_preparation(project,session,message,self._safe(preparation))
