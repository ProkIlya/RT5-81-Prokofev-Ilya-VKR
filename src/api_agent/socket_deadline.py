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

    def __enter__(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("network deadline exhausted")
        self.timer = threading.Timer(remaining, self._interrupt)
        self.timer.daemon = True
        self.timer.start()
        return self

    def _interrupt(self):
        # shutdown пробуждает recv/readline даже при потоке частых маленьких
        # пакетов. Основной поток затем переводит обмен в time_limit.
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
        # После выхода таймер не должен закрыть уже переданный следующему
        # действию дескриптор. join исключает гонку cancel с _interrupt.
        self.timer.cancel()
        self.timer.join()
