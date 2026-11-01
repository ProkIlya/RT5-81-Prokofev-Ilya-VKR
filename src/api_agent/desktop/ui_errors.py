"""Ожидаемый отказ read API отображается без обхода integrity и без recovery."""
from functools import wraps
from api_agent.desktop.presentation import error_text


def read_feedback(method):
    @wraps(method)
    def guarded(self,*args,**kwargs):
        try:return method(self,*args,**kwargs)
        except ValueError as error:self.set_status('Ошибка просмотра: '+error_text(error))
    return guarded
