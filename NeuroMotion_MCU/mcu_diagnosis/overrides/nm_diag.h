#ifndef NM_DIAG_H
#define NM_DIAG_H
#include "main.h"
#include <stdint.h>

#define NM_DIAG_MAGIC 0x4E4D4447U
#define NM_STAGE_BOOT 1U
#define NM_STAGE_WAIT 2U
#define NM_STAGE_RUNNING 3U
#define NM_STAGE_COMPLETE 4U
#define NM_STAGE_ERROR 5U
#define NM_STAGE_PROBING 6U
#define NM_STAGE_PROBED 7U

/* 64 aligned words: one bounded SWD read; counters are live observations. */
typedef struct {
  uint32_t magic, version, mode, stage;
  uint32_t tick_ms, heartbeat, init_status, device_id;
  uint32_t irq_enter, irq_exit, spi_ok, spi_error;
  uint32_t samples, buffer_bytes, dropped, tx_attempt;
  uint32_t tx_ok, tx_error, last_hal_status, last_phase;
  uint32_t start_ms, finish_ms, last_irq_ms, last_spi_ms;
  uint32_t max_late_ms, fault_tick, raw_status, drdy_level;
  uint32_t start_level, pwdn_level, reserved0, ads_error;
  uint32_t gpio_phase, gpio_frequency_hz, gpio_low_edges, gpio_high_edges;
  uint32_t gpio_low_duration_ms, gpio_high_duration_ms, gpio_reserved0, gpio_reserved1;
  uint32_t registers_0_3, registers_4_7, registers_8_11;
  uint32_t response_0_3, response_4_7, response_8;
  uint32_t valid_status, invalid_status;
  uint32_t probe_reason, probe_stage, probe_tick_ms, probe_stop_status;
  uint32_t probe_sdatac_status, probe_read_status;
  uint32_t post_registers_0_3, post_registers_4_7, post_registers_8_11;
  uint32_t post_ids_0_3, probe_id_status_bits, probe_levels_before;
  uint32_t post_repeat_0_3, post_repeat_4_7, post_repeat_8_11, probe_repeat_read_status;
} NM_Diag;
extern volatile NM_Diag g_nm_diag;
void NM_DiagInit(uint32_t mode);
void NM_DiagHeartbeat(void);
void NM_DiagUartRun(void);
void NM_DiagGpioRun(void);
#ifdef MCU_DIAG_REGCHECK
void ADS1292_DiagProbe(uint32_t reason);
#endif
#ifdef MCU_DIAG_ID_ONLY
void ADS1292_DiagIdRun(void);
#endif
#ifdef MCU_DIAG_SPI_LOOPBACK
typedef struct {
  uint32_t magic, version, state, rates_done;
  uint32_t transfers, bytes_checked, hal_errors, mismatched_bytes;
  uint32_t first_bad_offset, expected_byte, received_byte, started_ms;
  uint32_t finished_ms, reserved0, reserved1, reserved2;
  struct {
    uint32_t divisor, transfers, hal_errors, mismatched_bytes, elapsed_ms;
  } rates[3];
  uint32_t reserved3;
} NM_SpiSelfTest;
extern volatile NM_SpiSelfTest g_nm_spi_selftest;
void NM_DiagSpiLoopbackRun(void);
#endif
#ifdef MCU_DIAG_SOFT_SPI
HAL_StatusTypeDef NM_DiagSoftSpi(const uint8_t *tx, uint8_t *rx, uint16_t length);
#endif
#endif
