"""Continuously drain the serial driver independently of analysis and disk I/O."""
from __future__ import annotations

import threading
import time


class SerialSource:
    kind = 'serial'

    def __init__(self, port: str, baudrate: int = 921600, *, buffer_capacity: int = 262144):
        import serial
        if buffer_capacity < 1:
            raise ValueError('Serial buffer capacity must be positive')
        self.serial = serial.Serial(port, baudrate, timeout=.05)
        try:
            if hasattr(self.serial, 'set_buffer_size'):
                self.serial.set_buffer_size(rx_size=262144, tx_size=4096)
        except Exception:
            self.serial.close()
            raise
        self.description = {'kind': self.kind, 'port': port, 'baudrate': baudrate, 'format': '8N1'}
        self.eof = False
        self._capacity = buffer_capacity
        self._buffer = bytearray()
        self._condition = threading.Condition()
        self._closing = threading.Event()
        self._error = None
        self._peak = 0
        self._max_poll_gap_s = 0.
        self._reader = threading.Thread(target=self._receive, name='NeuroMotion-serial-reader', daemon=True)
        self._reader.start()

    def _receive(self):
        previous = time.monotonic()
        try:
            while not self._closing.is_set():
                # Batch at least one small packet without waiting more than the
                # serial timeout. The reader never parses, fits, plots or writes.
                data = self.serial.read(min(4096, max(256, self.serial.in_waiting)))
                now = time.monotonic()
                with self._condition:
                    self._max_poll_gap_s = max(self._max_poll_gap_s, now-previous)
                    previous = now
                    if len(self._buffer)+len(data) > self._capacity:
                        raise BufferError('Serial receive buffer full; recording cannot be guaranteed lossless')
                    self._buffer.extend(data)
                    self._peak = max(self._peak, len(self._buffer))
                    self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                if not self._closing.is_set():
                    self._error = exc
                self._condition.notify_all()

    @property
    def in_waiting(self):
        with self._condition:
            if self._error is not None:
                raise self._error
            return len(self._buffer)

    @property
    def diagnostics(self):
        with self._condition:
            return {'receive_buffer_capacity': self._capacity,
                    'receive_buffer_peak_bytes': self._peak,
                    'reader_max_poll_gap_s': self._max_poll_gap_s,
                    'reader_error': str(self._error) if self._error else ''}

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b''
        with self._condition:
            if not self._buffer and self._error is None and not self._closing.is_set():
                self._condition.wait(.05)
            if self._error is not None:
                raise self._error
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            return data

    def close(self):
        if self.eof:
            return
        self._closing.set()
        with self._condition:
            self._condition.notify_all()
        cancel = getattr(self.serial, 'cancel_read', None)
        if cancel is not None and getattr(self.serial, 'is_open', True):
            try:
                cancel()
            except OSError:
                pass  # Already disconnected; retain the receive error above.
        self._reader.join(timeout=1)
        self.serial.close()
        if self._reader.is_alive():
            self._reader.join(timeout=1)
        if self._reader.is_alive():
            raise RuntimeError('Serial reader did not terminate')
        self.eof = True
