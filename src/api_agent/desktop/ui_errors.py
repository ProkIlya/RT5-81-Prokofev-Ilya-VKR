"""Ожидаемый отказ read API отображается без обхода integrity и без recovery."""
from functools import wraps


def read_feedback(method):
    @wraps(method)
    def guarded(self,*args,**kwargs):
        try:return method(self,*args,**kwargs)
        except ValueError as error:self.set_status('Просмотр пока недоступен: '+str(error))
    return guarded
