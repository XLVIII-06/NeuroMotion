"""Bench-only diagnostic decoding; synthetic data never count as real EMG."""
from __future__ import annotations

import struct
from .acquisition import HEADER, FRAME_SIZE, RECORD

DIAG_FIELDS = ("magic version mode stage tick_ms heartbeat init_status device_id "
               "irq_enter irq_exit spi_ok spi_error samples buffer_bytes dropped tx_attempt "
               "tx_ok tx_error last_hal_status last_phase start_ms finish_ms last_irq_ms last_spi_ms "
               "max_late_ms fault_tick raw_status drdy_level start_level pwdn_level reserved0 ads_error").split()
GPIO_FIELDS = ("gpio_phase gpio_frequency_hz gpio_low_edges gpio_high_edges "
               "gpio_low_duration_ms gpio_high_duration_ms gpio_reserved0 gpio_reserved1").split()
SPI_FIELDS = ("registers_0_3 registers_4_7 registers_8_11 response_0_3 response_4_7 response_8 "
              "valid_status invalid_status").split()
PROBE_FIELDS = ("probe_reason probe_stage probe_tick_ms probe_stop_status probe_sdatac_status probe_read_status "
                "post_registers_0_3 post_registers_4_7 post_registers_8_11 post_ids_0_3 probe_id_status_bits "
                "probe_levels_before post_repeat_0_3 post_repeat_4_7 post_repeat_8_11 probe_repeat_read_status").split()


def decode_diag(data):
    if len(data) not in (128, 160, 192, 256):
        raise ValueError("Diagnostic RAM block must contain 128, 160, 192 or 256 bytes")
    fields = (DIAG_FIELDS + (GPIO_FIELDS if len(data) >= 160 else []) +
              (SPI_FIELDS if len(data) >= 192 else []) + (PROBE_FIELDS if len(data) == 256 else []))
    result = dict(zip(fields, struct.unpack(f"<{len(fields)}I", data)))
    if result["magic"] != 0x4E4D4447 or (result["version"], len(data)) not in ((1, 128), (2, 160), (3, 192), (4, 256)):
        raise ValueError("Diagnostic signature/version does not match")
    return result


def decode_spi_loopback(data):
    if len(data) != 128:
        raise ValueError("SPI loopback result must contain 128 bytes")
    fields = ("magic version state rates_done transfers bytes_checked hal_errors mismatched_bytes "
              "first_bad_offset expected_byte received_byte started_ms finished_ms reserved0 reserved1 reserved2").split()
    words = struct.unpack("<32I", data)
    result = dict(zip(fields, words[:16]))
    if result["magic"] != 0x4E4D5350 or result["version"] != 1:
        raise ValueError("SPI loopback signature/version does not match")
    result["rates"] = [dict(zip(("divisor", "transfers", "hal_errors", "mismatched_bytes", "elapsed_ms"),
                              words[16+i*5:21+i*5])) for i in range(3)]
    passed = (result["state"] == 3 and result["rates_done"] == 3 and
              result["transfers"] == 3072 and result["bytes_checked"] == 36864 and
              result["hal_errors"] == 0 and result["mismatched_bytes"] == 0 and
              [r["divisor"] for r in result["rates"]] == [256, 128, 64] and
              all(r["transfers"] == 1024 and r["hal_errors"] == r["mismatched_bytes"] == 0 for r in result["rates"]))
    result["status"] = "running" if result["state"] in (1, 2) else "passed" if passed else "failed"
    result["real_adc_validated"] = False
    return result


def validate_uart_binary(raw: bytes, total_samples: int = 120000):
    """Find a complete reset-delimited run, checking bytes and exact truth."""
    frames, cursor = [], 0
    while cursor < len(raw):
        start = raw.find(HEADER, cursor)
        if start < 0 or start + FRAME_SIZE > len(raw):
            break
        if raw[start + FRAME_SIZE - 1] != 0x5A:
            cursor = start + 1
            continue
        frames.append((start, list(RECORD.iter_unpack(raw[start + 4:start + 244]))))
        cursor = start + FRAME_SIZE
    candidates = [i for i, (_, rows) in enumerate(frames) if rows[0][0] == 0]
    count = total_samples // 12
    failures = []
    for begin in reversed(candidates):
        selected = frames[begin:begin + count]
        if len(selected) != count:
            failures.append("incomplete run")
            continue
        error = None
        for block, (offset, rows) in enumerate(selected):
            if offset != selected[0][0] + block * FRAME_SIZE:
                error = "missing/corrupt bytes inside run"
                break
            for position, (index, ch1, ch2, ttl, dropped) in enumerate(rows):
                expected = block * 12 + position
                value = (expected % 1000 - 500) * .25
                if index != expected:
                    error = f"sample index discontinuity at {expected}: {index}"
                    break
                if (struct.pack("<f", ch1) != struct.pack("<f", value) or
                        struct.pack("<f", ch2) != struct.pack("<f", -value)):
                    error = f"synthetic channel truth mismatch at {expected}"
                    break
                if ttl != (expected // 500) % 2 or dropped != 0:
                    error = f"TTL/drop truth mismatch at {expected}"
                    break
            if error:
                break
        if error is None:
            start = selected[0][0]
            end = selected[-1][0] + FRAME_SIZE
            return {"status": "passed", "synthetic_uart_only": True, "samples": total_samples,
                    "frames": count, "run_bytes": end - start, "run_byte_start": start,
                    "run_byte_end": end, "excluded_prefix_bytes": start,
                    "excluded_suffix_bytes": len(raw) - end}
        failures.append(error)
    return {"status": "failed" if any(item != "incomplete run" for item in failures) else "incomplete",
            "synthetic_uart_only": True, "reason": failures[0] if failures else "no frame with sample index 0",
            "observed_frames": len(frames), "run_candidates": len(candidates)}
