"""Приложение классифицирует bounded заголовки до persistence/trace/наблюдений."""
import json
import re


PUBLIC_HEADERS=frozenset({'accept','accept-encoding','accept-language','allow','cache-control',
    'connection','content-encoding','content-language','content-length','content-type','date',
    'etag','expires','host','last-modified','retry-after','server','transfer-encoding','vary',
    'x-request-id','x-correlation-id','traceparent','tracestate'})
SECRET_NAME=re.compile(r'auth|cookie|token|secret|password|credential|private|owner|control|key',re.I)
HEADER_NAME=re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")


def public_response_headers(value):
    """Расширение только из доверенной mapping; закрытый класс переопределять нельзя."""
    if not isinstance(value,list) or len(value)>48 or any(not isinstance(n,str)
        or not HEADER_NAME.fullmatch(n) or SECRET_NAME.search(n) for n in value):
        raise ValueError('invalid_public_response_headers')
    return frozenset(n.lower() for n in value)


def request_headers(port,method,body,supplied):
    """Явно задаём также поля, которые HTTPConnection иначе добавляет неявно."""
    headers={'Host':'127.0.0.1'+(':'+str(port) if port!=80 else ''),
        'Accept-Encoding':'identity','Accept':'application/json',**supplied}
    if body is not None:headers.update({'Content-Type':'application/json','Content-Length':str(len(body))})
    elif method in {'POST','PUT','PATCH'}:headers['Content-Length']='0'
    return headers


def classified_headers(pairs,*,public=(),redact=lambda value:value):
    """Сохраняем порядок/повторы, маски и явные границы вместо сырого header dump."""
    result={'version':'classified-headers-v1','items':[],'omitted':0,'truncated':False}
    allowed=PUBLIC_HEADERS|frozenset(n.lower() for n in public)
    for name,value in pairs:
        if len(result['items'])>=48:
            result['omitted']+=1;result['truncated']=True;continue
        if not HEADER_NAME.fullmatch(name):
            result['omitted']+=1;result['truncated']=True;continue
        if SECRET_NAME.search(name) or re.search(r'\b(?:Bearer|Basic)\s+\S+',value,re.I):
            classification,value='secret','[REDACTED]'
        elif name.lower() not in allowed:classification,value='unclassified','[NOT_CLASSIFIED]'
        else:classification,value='public',redact(value)
        encoded=value.encode('utf-8')
        cut=len(encoded)>1024
        if cut:value=encoded[:1024].decode('utf-8',errors='ignore');result['truncated']=True
        item={'name':redact(name),'value':value,'classification':classification}
        if cut:item['truncated']=True
        result['items'].append(item)
        if len(json.dumps(result,ensure_ascii=True).encode())>8192:
            result['items'].pop();result['omitted']+=1;result['truncated']=True
    # Итоговый счётчик omitted сам растёт; проверяем уже окончательный envelope.
    while len(json.dumps(result,ensure_ascii=True).encode())>8192:
        result['items'].pop();result['omitted']+=1;result['truncated']=True
    return result
