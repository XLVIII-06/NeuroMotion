#include "nm_diag.h"
#include "usart.h"
#include <string.h>
#ifdef MCU_DIAG_SPI_LOOPBACK
#include "spi.h"
#endif
#if defined(MCU_DIAG_GPIO)
#include "tim.h"
#endif

volatile NM_Diag g_nm_diag;
typedef char nm_diag_size_check[(sizeof(NM_Diag) == 256U) ? 1 : -1];
static uint32_t last_heartbeat;

#ifdef MCU_DIAG_SPI_LOOPBACK
volatile NM_SpiSelfTest g_nm_spi_selftest;
typedef char nm_spi_size_check[(sizeof(NM_SpiSelfTest) == 128U) ? 1 : -1];

void NM_DiagSpiLoopbackRun(void)
{
  static const uint8_t pattern[12] = {0x00U, 0xFFU, 0xAAU, 0x55U, 0x01U, 0x80U,
                                    0x7FU, 0xFEU, 0x3CU, 0xC3U, 0x96U, 0x69U};
  static const uint32_t dividers[3] = {SPI_BAUDRATEPRESCALER_256, SPI_BAUDRATEPRESCALER_128,
                                      SPI_BAUDRATEPRESCALER_64};
  static const uint32_t values[3] = {256U, 128U, 64U};
  uint8_t tx[12], rx[12];
  NM_DiagInit(7U);
  HAL_NVIC_DisableIRQ(EXTI9_5_IRQn);
  memset((void *)&g_nm_spi_selftest, 0, sizeof(g_nm_spi_selftest));
  g_nm_spi_selftest.magic = 0x4E4D5350U;
  g_nm_spi_selftest.version = 1U;
  g_nm_spi_selftest.state = 1U; /* Wait for the operator's reset. */
  g_nm_spi_selftest.first_bad_offset = 0xFFFFFFFFU;
  g_nm_diag.stage = NM_STAGE_WAIT;
  HAL_Delay(5000U);
  g_nm_diag.stage = NM_STAGE_RUNNING;
  g_nm_spi_selftest.state = 2U;
  g_nm_spi_selftest.started_ms = HAL_GetTick();
  for (uint32_t rate = 0U; rate < 3U; rate++) {
    uint32_t started = HAL_GetTick();
    g_nm_spi_selftest.rates[rate].divisor = values[rate];
    __HAL_SPI_DISABLE(&hspi1);
    hspi1.Init.BaudRatePrescaler = dividers[rate];
    HAL_StatusTypeDef status = HAL_SPI_Init(&hspi1);
    if (status != HAL_OK) {
      g_nm_spi_selftest.hal_errors++;
      g_nm_spi_selftest.rates[rate].hal_errors++;
    } else {
      for (uint32_t trial = 0U; trial < 1024U; trial++) {
        for (uint32_t i = 0U; i < sizeof(tx); i++) { tx[i] = pattern[i] ^ (uint8_t)trial; }
        memset(rx, 0, sizeof(rx));
        status = HAL_SPI_TransmitReceive(&hspi1, tx, rx, sizeof(tx), 10U);
        g_nm_spi_selftest.transfers++;
        g_nm_spi_selftest.rates[rate].transfers++;
        g_nm_diag.last_hal_status = (uint32_t)status;
        if (status != HAL_OK) {
          g_nm_spi_selftest.hal_errors++;
          g_nm_spi_selftest.rates[rate].hal_errors++;
        } else {
          for (uint32_t i = 0U; i < sizeof(tx); i++) {
            if (rx[i] != tx[i]) {
              if (g_nm_spi_selftest.first_bad_offset == 0xFFFFFFFFU) {
                g_nm_spi_selftest.first_bad_offset = g_nm_spi_selftest.bytes_checked + i;
                g_nm_spi_selftest.expected_byte = tx[i];
                g_nm_spi_selftest.received_byte = rx[i];
              }
              g_nm_spi_selftest.mismatched_bytes++;
              g_nm_spi_selftest.rates[rate].mismatched_bytes++;
            }
          }
          g_nm_spi_selftest.bytes_checked += sizeof(tx);
        }
        NM_DiagHeartbeat();
      }
    }
    g_nm_spi_selftest.rates[rate].elapsed_ms = HAL_GetTick() - started;
    g_nm_spi_selftest.rates_done = rate + 1U;
  }
  g_nm_spi_selftest.finished_ms = HAL_GetTick();
  uint8_t passed = (g_nm_spi_selftest.hal_errors == 0U && g_nm_spi_selftest.mismatched_bytes == 0U);
  g_nm_spi_selftest.state = passed ? 3U : 4U;
  g_nm_diag.stage = passed ? NM_STAGE_COMPLETE : NM_STAGE_ERROR;
  for (;;) { NM_DiagHeartbeat(); }
}
#endif

#ifdef MCU_DIAG_SOFT_SPI
static uint8_t soft_spi_ready;

static uint8_t soft_spi_half_period(void)
{
  uint32_t start = DWT->CYCCNT;
  uint32_t cycles = SystemCoreClock / 250000U; /* At least 4 us per half cycle. */
  for (uint32_t spins = 0U; spins < 8192U; spins++) {
    if ((uint32_t)(DWT->CYCCNT - start) >= cycles) { return 1U; }
  }
  return 0U; /* Bounded even if the cycle counter is unavailable. */
}

HAL_StatusTypeDef NM_DiagSoftSpi(const uint8_t *tx, uint8_t *rx, uint16_t length)
{
  if (soft_spi_ready == 0U) {
    GPIO_InitTypeDef gpio = {0};
    CLEAR_BIT(SPI1->CR1, SPI_CR1_SPE);
    __HAL_RCC_GPIOA_CLK_ENABLE();
    GPIOA->BSRR = (GPIO_PIN_5 | GPIO_PIN_7) << 16;
    gpio.Pin = GPIO_PIN_5 | GPIO_PIN_7;
    gpio.Mode = GPIO_MODE_OUTPUT_PP;
    gpio.Pull = GPIO_NOPULL;
    gpio.Speed = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOA, &gpio);
    gpio.Pin = GPIO_PIN_6;
    gpio.Mode = GPIO_MODE_INPUT;
    HAL_GPIO_Init(GPIOA, &gpio);
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    if (soft_spi_half_period() == 0U) { return HAL_ERROR; }
    soft_spi_ready = 1U;
  }
  for (uint16_t i = 0U; i < length; i++) {
    uint8_t received = 0U, sent = tx[i];
    for (uint8_t bit = 0U; bit < 8U; bit++) {
      GPIOA->BSRR = (sent & 0x80U) != 0U ? GPIO_PIN_7 : (GPIO_PIN_7 << 16);
      GPIOA->BSRR = GPIO_PIN_5;
      if (soft_spi_half_period() == 0U) { return HAL_TIMEOUT; }
      GPIOA->BSRR = GPIO_PIN_5 << 16;
      if (soft_spi_half_period() == 0U) { return HAL_TIMEOUT; }
      received = (uint8_t)((received << 1) | ((GPIOA->IDR & GPIO_PIN_6) != 0U ? 1U : 0U));
      sent <<= 1;
    }
    rx[i] = received;
  }
  GPIOA->BSRR = GPIO_PIN_7 << 16;
  return HAL_OK;
}
#endif

void NM_DiagInit(uint32_t mode)
{
  memset((void *)&g_nm_diag, 0, sizeof(g_nm_diag));
  g_nm_diag.magic = NM_DIAG_MAGIC;
  g_nm_diag.version = 4U;
  g_nm_diag.mode = mode;
  g_nm_diag.stage = NM_STAGE_BOOT;
  g_nm_diag.init_status = 0xFFFFFFFFU; /* ADS not tested in UART-only mode. */
  last_heartbeat = HAL_GetTick();
}

void NM_DiagHeartbeat(void)
{
  uint32_t now = HAL_GetTick();
  if (now != last_heartbeat) {
    last_heartbeat = now;
    g_nm_diag.tick_ms = now;
    g_nm_diag.heartbeat++;
    g_nm_diag.drdy_level = (uint32_t)HAL_GPIO_ReadPin(ADS1292_DRDY_GPIO_Port, ADS1292_DRDY_Pin);
    g_nm_diag.start_level = (uint32_t)HAL_GPIO_ReadPin(ADS1292_START_GPIO_Port, ADS1292_START_Pin);
    g_nm_diag.pwdn_level = (uint32_t)HAL_GPIO_ReadPin(ADS1292_PWDN_GPIO_Port, ADS1292_PWDN_Pin);
  }
}

static void write_u32(uint8_t *dest, uint32_t value)
{
  dest[0] = (uint8_t)value; dest[1] = (uint8_t)(value >> 8);
  dest[2] = (uint8_t)(value >> 16); dest[3] = (uint8_t)(value >> 24);
}

static void write_float(uint8_t *dest, float value)
{
  uint32_t bits;
  memcpy(&bits, &value, sizeof(bits));
  write_u32(dest, bits);
}

void NM_DiagUartRun(void)
{
  uint8_t frame[245];
  uint32_t start, index = 0U;
  NM_DiagInit(1U);
  HAL_NVIC_DisableIRQ(EXTI9_5_IRQn);
  g_nm_diag.stage = NM_STAGE_WAIT;
  start = HAL_GetTick();
  while ((HAL_GetTick() - start) < 5000U) { NM_DiagHeartbeat(); }
  start = HAL_GetTick();
  g_nm_diag.start_ms = start;
  g_nm_diag.stage = NM_STAGE_RUNNING;
  frame[0] = 0xA5U; frame[1] = 0xF2U; frame[2] = 0x11U; frame[3] = 0xE3U;
  frame[244] = 0x5AU;
  for (uint32_t block = 0U; block < 10000U; block++) {
    uint32_t deadline = start + (block + 1U) * 12U;
    while ((int32_t)(HAL_GetTick() - deadline) < 0) { NM_DiagHeartbeat(); }
    uint32_t late = HAL_GetTick() - deadline;
    if (late > g_nm_diag.max_late_ms) { g_nm_diag.max_late_ms = late; }
    for (uint32_t row = 0U; row < 12U; row++, index++) {
      uint8_t *record = &frame[4U + row * 20U];
      float value = (float)((int32_t)(index % 1000U) - 500) * 0.25f;
      write_u32(record, index);
      write_float(&record[4], value);
      write_float(&record[8], -value);
      record[12] = (uint8_t)((index / 500U) & 1U);
      record[13] = record[14] = record[15] = 0U;
      write_u32(&record[16], 0U);
    }
    g_nm_diag.samples = index;
    g_nm_diag.last_phase = 10U; /* About to enter UART transmit. */
    g_nm_diag.tx_attempt++;
    HAL_StatusTypeDef result = HAL_UART_Transmit(&huart2, frame, sizeof(frame), 1000U);
    g_nm_diag.last_hal_status = (uint32_t)result;
    g_nm_diag.last_phase = 11U; /* UART transmit returned. */
    if (result != HAL_OK) {
      g_nm_diag.tx_error++;
      g_nm_diag.stage = NM_STAGE_ERROR;
      g_nm_diag.fault_tick = HAL_GetTick();
      break;
    }
    g_nm_diag.tx_ok++;
    NM_DiagHeartbeat();
  }
  if (g_nm_diag.stage != NM_STAGE_ERROR) { g_nm_diag.stage = NM_STAGE_COMPLETE; }
  g_nm_diag.finish_ms = HAL_GetTick();
  for (;;) { NM_DiagHeartbeat(); }
}

#if defined(MCU_DIAG_GPIO)
static void gpio_frequency(uint32_t hz)
{
  HAL_TIM_PWM_Stop(&htim1, TIM_CHANNEL_1);
  __HAL_TIM_SET_PRESCALER(&htim1, 8399U);
  __HAL_TIM_SET_AUTORELOAD(&htim1, hz == 1U ? 9999U : 9U);
  __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, hz == 1U ? 5000U : 5U);
  __HAL_TIM_SET_COUNTER(&htim1, 0U);
  htim1.Instance->EGR = TIM_EGR_UG;
  __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
  HAL_StatusTypeDef status = HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_1);
  g_nm_diag.last_hal_status = (uint32_t)status;
  g_nm_diag.gpio_frequency_hz = hz;
  if (status != HAL_OK) { g_nm_diag.stage = NM_STAGE_ERROR; }
}

static void gpio_wait(uint32_t milliseconds)
{
  uint32_t start = HAL_GetTick();
  while ((HAL_GetTick() - start) < milliseconds) { NM_DiagHeartbeat(); }
}

void NM_DiagGpioRun(void)
{
  uint32_t first, start;
  NM_DiagInit(3U);
  g_nm_diag.stage = NM_STAGE_WAIT;
  gpio_wait(5000U);
  g_nm_diag.stage = NM_STAGE_RUNNING;
  g_nm_diag.start_ms = HAL_GetTick();
  g_nm_diag.gpio_phase = 1U;
  gpio_frequency(1U);
  gpio_wait(1100U);
  g_nm_diag.gpio_phase = 2U;
  first = g_nm_diag.irq_enter;
  start = HAL_GetTick();
  gpio_wait(10000U);
  g_nm_diag.gpio_low_duration_ms = HAL_GetTick() - start;
  g_nm_diag.gpio_low_edges = g_nm_diag.irq_enter - first;
  g_nm_diag.gpio_phase = 3U;
  gpio_frequency(1000U);
  gpio_wait(20U);
  g_nm_diag.gpio_phase = 4U;
  first = g_nm_diag.irq_enter;
  start = HAL_GetTick();
  gpio_wait(10000U);
  g_nm_diag.gpio_high_duration_ms = HAL_GetTick() - start;
  g_nm_diag.gpio_high_edges = g_nm_diag.irq_enter - first;
  HAL_NVIC_DisableIRQ(EXTI9_5_IRQn);
  HAL_TIM_PWM_Stop(&htim1, TIM_CHANNEL_1);
  GPIO_InitTypeDef idle = {0};
  idle.Pin = GPIO_PIN_8;
  idle.Mode = GPIO_MODE_OUTPUT_PP;
  idle.Pull = GPIO_NOPULL;
  idle.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOA, &idle);
  HAL_GPIO_WritePin(GPIOA, GPIO_PIN_8, GPIO_PIN_RESET);
  g_nm_diag.gpio_frequency_hz = 0U;
  g_nm_diag.gpio_phase = 5U;
  g_nm_diag.finish_ms = HAL_GetTick();
  if (g_nm_diag.stage != NM_STAGE_ERROR) { g_nm_diag.stage = NM_STAGE_COMPLETE; }
  for (;;) { NM_DiagHeartbeat(); }
}
#endif
