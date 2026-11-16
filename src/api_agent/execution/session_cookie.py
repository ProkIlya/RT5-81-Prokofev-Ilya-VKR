"""Одна именованная cookie в доверенном HTTP-контексте Run."""
from http.cookiejar import CookieJar
from urllib.request import Request

from api_agent.execution.http_metadata import HEADER_NAME


def cookie_schemes(operation):
    return {scheme['name'] for alternative in operation['security'] for scheme in alternative.values()
            if scheme.get('type') == 'apiKey' and scheme.get('in') == 'cookie'}


def cookie_name_valid(name):
    return isinstance(name, str) and HEADER_NAME.fullmatch(name) is not None and not name.startswith('$')


class SessionCookie:
    def __init__(self, name):
        self.name = name
        self.current = CookieJar()
        self.pending = None

    def capture(self, response, url, protect):
        request = Request(url)
        candidate = CookieJar()
        # Даже значение отклонённой cookie может быть отражено сервером в JSON.
        for header in response.info().get_all('Set-Cookie', []):
            _, separator, value = header.partition(';')[0].partition('=')
            if separator:
                protect(value.strip().strip('"'))
        cookies = candidate.make_cookies(response, request)
        selected = [cookie for cookie in cookies if cookie.name == self.name]
        if len(selected) == 1:
            cookie = selected[0]
            value = cookie.value
            if value and len(value) <= 4096 and all(33 <= ord(c) <= 126 and c not in '\",;\\' for c in value):
                candidate.set_cookie_if_ok(cookie, request)
        self.pending = candidate

    def accept(self, valid):
        self.current = self.pending if valid and self.pending is not None else CookieJar()
        self.pending = None
        return bool(len(self.current))

    def header(self, url):
        request = Request(url)
        self.current.add_cookie_header(request)
        return request.get_header('Cookie')

    @property
    def active(self):
        return bool(len(self.current))

    def clear(self):
        self.current.clear()
        self.pending = None
