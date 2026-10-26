"""Публичная конфигурация прав Project и совместимый доступ к policy session."""
from api_agent.policy.config import ProjectConfig, PolicyStop, decision


def __getattr__(name):
    if name=='PolicySession':
        from api_agent.execution.session import PolicySession
        return PolicySession
    raise AttributeError(name)
