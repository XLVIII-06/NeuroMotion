"""A small driver buffer must not depend on the analysis consumer's pace."""
import threading
import time
import unittest
from unittest.mock import patch

from neuromotion.acquisition import SerialSource


class LimitedDriver:
    def __init__(self, capacity=512):
        self.capacity = capacity
        self.buffer = bytearray()
        self.condition = threading.Condition()
        self.closed = False
        self.lost = 0
        self.failure = None

    def set_buffer_size(self, **kwargs):
        pass  # A driver's advisory request does not change this test limit.

    @property
    def in_waiting(self):
        with self.condition:
            if self.failure:
                raise self.failure
            return len(self.buffer)

    def feed(self, data):
        with self.condition:
            available = self.capacity-len(self.buffer)
            self.lost += max(0, len(data)-available)
            self.buffer.extend(data[:available])
            self.condition.notify_all()

    def read(self, size):
        with self.condition:
            if not self.buffer and not self.closed:
                self.condition.wait(.01)
            if self.failure:
                raise self.failure
            data = bytes(self.buffer[:size])
            del self.buffer[:size]
            return data

    def cancel_read(self):
        with self.condition:
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class SerialTransportTests(unittest.TestCase):
    def open_source(self, driver, **kwargs):
        with patch('serial.Serial', return_value=driver):
            return SerialSource('TEST', **kwargs)

    def test_slow_consumer_preserves_exact_bytes_from_limited_driver(self):
        driver = LimitedDriver()
        source = self.open_source(driver)
        payload = bytes(range(256))*64
        def produce():
            for offset in range(0, len(payload), 128):
                driver.feed(payload[offset:offset+128])
                time.sleep(.003)
        producer = threading.Thread(target=produce)
        try:
            producer.start()
            producer.join(3)  # No consumer reads while the driver keeps receiving.
            self.assertFalse(producer.is_alive())
            received = bytearray()
            deadline = time.monotonic()+1
            while len(received) < len(payload) and time.monotonic() < deadline:
                received.extend(source.read(333))
            self.assertEqual(driver.lost, 0)
            self.assertEqual(bytes(received), payload)
        finally:
            source.close()
            producer.join(3)

    def test_driver_failure_reaches_consumer_and_closes(self):
        driver = LimitedDriver()
        source = self.open_source(driver)
        try:
            driver.failure = OSError('device disconnected')
            deadline = time.monotonic()+1
            with self.assertRaisesRegex(OSError, 'device disconnected'):
                while time.monotonic() < deadline:
                    source.read(1)
        finally:
            source.close()
        self.assertTrue(driver.closed)
        source.close()
        self.assertTrue(source.eof)

    def test_overflow_is_explicit_and_buffer_stays_bounded(self):
        driver = LimitedDriver(capacity=4096)
        source = self.open_source(driver, buffer_capacity=512)
        try:
            driver.feed(b'x'*1024)
            deadline = time.monotonic()+1
            with self.assertRaisesRegex(BufferError, 'buffer full'):
                while time.monotonic() < deadline:
                    time.sleep(.005)
                    _ = source.in_waiting
            self.assertLessEqual(source.diagnostics['receive_buffer_peak_bytes'], 512)
        finally:
            source.close()

    def test_acceptance_checks_receiver_even_when_mcu_counters_are_zero(self):
        from scripts.validate_four_channel_serial import require_healthy_stream
        good = dict(error='', clock={'hardware': {'late_a': 0, 'max_read_delay_us': 500}})
        require_healthy_stream(good)
        for key in ('frame_errors', 'invalid_records', 'index_gaps', 'packet_gaps'):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'Receiver data loss'):
                require_healthy_stream({**good, key: 1})

    def test_only_pre_recording_resync_is_separate_from_capture_errors(self):
        from scripts.validate_four_channel_serial import require_healthy_stream
        baseline = dict(frame_errors=2, index_gaps=0, invalid_records=0, packet_gaps=0)
        state = dict(error='', frame_errors=2)
        require_healthy_stream(state, baseline)
        with self.assertRaisesRegex(RuntimeError, 'Receiver data loss'):
            require_healthy_stream({**state, 'frame_errors': 3}, baseline)


if __name__ == '__main__':
    unittest.main()
