"""Абсолютный срок блокирующего сетевого обмена на локальном сокете.

Обычный socket timeout измеряет ожидание очередной порции данных. Сервер,
который регулярно передаёт по байту, может держать getresponse/readline дольше
бюджета Run. Таймер прерывает уже выполняющееся чтение через shutdown().
"""
import socket
import threading
import time


class SocketDeadline:
    """Ограничить весь обмен на одном сокете абсолютным monotonic deadline."""

    def __init__(self, connection_socket, deadline):
        self.connection_socket = connection_socket
        self.deadline = deadline
        self.expired = False
        self.timer = None
        self._lock = threading.Lock()
        self._closed = False

    def __enter__(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("network deadline exhausted")
        with self._lock:
            self._arm(remaining)
        return self

    def _arm(self, remaining):
        # Вызывается под lock: выход и перевзвод не теряют текущий таймер.
        self.timer = threading.Timer(remaining, self._interrupt)
        self.timer.daemon = True
        self.timer.start()

    def _interrupt(self):
        with self._lock:
            if self._closed or self.expired:
                return
            # Пробуждение Timer само по себе не доказывает истечение срока:
            # системное ожидание может завершиться раньше monotonic deadline.
            remaining = self.deadline - time.monotonic()
            if remaining > 0:
                self._arm(remaining)
                return
            # shutdown пробуждает recv/readline даже при частых малых пакетах.
            self.expired = True
            try:
                self.connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def check(self):
        """Не принимать ответ, если срок истёк на границе сетевой операции."""
        if self.expired or time.monotonic() >= self.deadline:
            raise TimeoutError("network deadline exhausted")

    def __exit__(self, *args):
        # Запрещаем перевзвод до cancel; join вне lock позволяет callback выйти.
        with self._lock:
            self._closed = True
            timer = self.timer
            timer.cancel()
        timer.join()
