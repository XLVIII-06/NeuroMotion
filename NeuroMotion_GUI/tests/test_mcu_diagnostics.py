import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from neuromotion.acquisition import encode_frame
from neuromotion.mcu_diagnostics import decode_diag, decode_spi_loopback, validate_uart_binary
from scripts.reset_mcu_diag import reset_diagnostic


def fixture(count=120):
    return b"".join(encode_frame((i, (i % 1000 - 500) * .25, -(i % 1000 - 500) * .25,
                                  (i // 500) % 2, 0) for i in range(start, start + 12))
                    for start in range(0, count, 12))


class DiagnosticTests(unittest.TestCase):
    def test_spi_loopback_requires_complete_error_free_wire_result(self):
        words = [0] * 32
        words[:8] = [0x4E4D5350, 1, 3, 3, 3072, 36864, 0, 0]
        for i, divisor in enumerate((256, 128, 64)):
            words[16+i*5:21+i*5] = [divisor, 1024, 0, 0, 100]
        self.assertEqual(decode_spi_loopback(struct.pack("<32I", *words))["status"], "passed")
        words[7] = 1
        self.assertEqual(decode_spi_loopback(struct.pack("<32I", *words))["status"], "failed")
        words[7] = 0
        words[4] = 1024
        self.assertEqual(decode_spi_loopback(struct.pack("<32I", *words))["status"], "failed")

    def test_spi_loopback_rejects_wrong_ram_and_retains_running_state(self):
        with self.assertRaises(ValueError):
            decode_spi_loopback(bytes(128))
        with self.assertRaises(ValueError):
            decode_spi_loopback(bytes(64))
        words = [0] * 32
        words[:3] = [0x4E4D5350, 1, 1]
        self.assertEqual(decode_spi_loopback(struct.pack("<32I", *words))["status"], "running")

    def test_complete_known_truth_with_reset_prefix_is_selected(self):
        partial = fixture(24)
        raw = b"noise" + partial + b"partial-reset" + fixture()
        result = validate_uart_binary(raw, 120)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["run_bytes"], 2450)
        self.assertEqual(result["excluded_prefix_bytes"], 5 + len(partial) + 13)

    def test_short_record_is_incomplete(self):
        self.assertEqual(validate_uart_binary(fixture(24), 120)["status"], "incomplete")

    def test_internal_byte_loss_is_rejected(self):
        raw = fixture()
        broken = raw[:400] + raw[401:]
        self.assertNotEqual(validate_uart_binary(broken, 120)["status"], "passed")

    def test_wrong_channel_truth_or_ttl_is_rejected(self):
        for offset, value in ((8, struct.pack("<f", 999)), (16, b"\x02")):
            raw = bytearray(fixture())
            raw[offset:offset + len(value)] = value
            self.assertEqual(validate_uart_binary(bytes(raw), 120)["status"], "failed")

    def test_ram_signature_and_size_are_checked(self):
        words = [0] * 32
        words[:4] = [0x4E4D4447, 1, 1, 4]
        self.assertEqual(decode_diag(struct.pack("<32I", *words))["stage"], 4)
        with self.assertRaises(ValueError):
            decode_diag(bytes(128))
        with self.assertRaises(ValueError):
            decode_diag(bytes(127))

    def test_gpio_ram_v2_and_old_uart_v1_remain_distinct(self):
        words = [0] * 40
        words[:4] = [0x4E4D4447, 2, 3, 4]
        words[34:36] = [10, 10000]
        snapshot = decode_diag(struct.pack("<40I", *words))
        self.assertEqual(snapshot["gpio_low_edges"], 10)
        self.assertEqual(snapshot["gpio_high_edges"], 10000)
        words[1] = 1
        with self.assertRaises(ValueError):
            decode_diag(struct.pack("<40I", *words))

    def test_spi_ram_v3_retains_register_response_and_validity(self):
        words = [0] * 48
        words[:4] = [0x4E4D4447, 3, 4, 3]
        words[40:48] = [0x7303A010, 0x00002F00, 0x4002030C, 0xC0000001, 0x02030405, 6, 1234, 7]
        decoded = decode_diag(struct.pack("<48I", *words))
        self.assertEqual(decoded["registers_0_3"], 0x7303A010)
        self.assertEqual(decoded["response_8"], 6)
        self.assertEqual(decoded["valid_status"], 1234)
        self.assertEqual(decoded["invalid_status"], 7)
        words[1] = 2
        with self.assertRaises(ValueError):
            decode_diag(struct.pack("<48I", *words))

    @patch("scripts.reset_mcu_diag.subprocess.run")
    @patch("scripts.reset_mcu_diag.run_cli", return_value="Flash differs")
    def test_hardware_reset_refuses_mismatched_image(self, compare, command):
        with TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                reset_diagnostic(Path(folder) / "test.hex", Path(folder) / "logs")
        command.assert_not_called()

    def test_v4_probe_keeps_initial_and_post_fault_registers_separate(self):
        words = [0] * 64
        words[:4] = [0x4E4D4447, 4, 4, 7]
        words[40] = 0x7303A010
        words[48:64] = [2, 4, 10080, 0, 0, 0, 0x7303A010, 0x2F00, 0x4002030C,
                        0x73737373, 0, 7, 0x7303A010, 0x2F00, 0x4002030C, 0]
        d = decode_diag(struct.pack("<64I", *words))
        self.assertEqual(d["registers_0_3"], 0x7303A010)
        self.assertEqual(d["probe_reason"], 2)
        self.assertEqual(d["post_ids_0_3"], 0x73737373)
        self.assertEqual(d["post_repeat_8_11"], 0x4002030C)
        self.assertEqual(d["probe_levels_before"], 7)
        words[1] = 3
        with self.assertRaises(ValueError):
            decode_diag(struct.pack("<64I", *words))

    @patch("scripts.reset_mcu_diag.subprocess.run")
    @patch("scripts.reset_mcu_diag.run_cli", return_value="No difference found.")
    def test_verified_hardware_reset_is_one_operation_and_redacts_probe_id(self, compare, command):
        command.return_value = SimpleNamespace(returncode=0, stdout=b"ST-LINK SN: test-device\nHard reset is performed.")
        with TemporaryDirectory() as folder:
            logs = Path(folder) / "logs"
            reset_diagnostic(Path(folder) / "test.hex", logs)
            self.assertNotIn("test-device", (logs / "hardware_reset.txt").read_text())
        self.assertEqual(command.call_args.args[0][1:], ["-c", "ID=0", "SWD", "HOTPLUG", "-HardRst"])
        command.assert_called_once()

    @patch("scripts.reset_mcu_diag.subprocess.run")
    @patch("scripts.reset_mcu_diag.run_cli", return_value="No difference found.")
    def test_hardware_reset_failure_does_not_silently_arm_observer(self, compare, command):
        command.return_value = SimpleNamespace(returncode=1, stdout=b"No target detected")
        with TemporaryDirectory() as folder:
            with self.assertRaises(RuntimeError):
                reset_diagnostic(Path(folder) / "test.hex", Path(folder) / "logs")


if __name__ == "__main__":
    unittest.main()
