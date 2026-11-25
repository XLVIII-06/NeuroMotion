#ifdef NEUROMOTION_DUAL_ADC
#include "dual_adc.h"
#include "main.h"
#include "spi.h"
#include "usart.h"
#include "sync_signal.h"
#include "nm_protocol_v2.h"
#include "nm_capture_map.h"
#include <stdio.h>
#include <string.h>

#define CAPACITY 128U
#define RECORDS 512U
#define EDGES 64U
typedef struct { uint64_t time; uint32_t index; uint8_t device; uint8_t fault; } Capture;
typedef struct { uint64_t time; uint32_t index; uint8_t level; } TTLEdge;
typedef struct {
  GPIO_TypeDef *cs_port; uint16_t cs_pin; uint16_t pwdn_pin;
  uint8_t registers[12]; uint8_t error;
  volatile uint32_t index, latest, dropped, overcapture, spi_errors, late, invalid_status;
} Device;
static Device dev[2];
static Capture captures[CAPACITY];
static volatile uint32_t cap_head, cap_tail;
static TTLEdge edges[EDGES];
static volatile uint32_t edge_head, edge_tail, edge_index, edge_dropped;
static volatile uint32_t edge_overcapture, edge_queue_dropped;
static uint8_t records[RECORDS][NM_V2_RECORD_SIZE];
static uint32_t rec_head, rec_tail, record_dropped, packet_sequence;
static uint8_t tx[NM_V2_MAX_PAYLOAD + 16U], payload[NM_V2_MAX_PAYLOAD];
static DMA_HandleTypeDef dma_tx;
static volatile uint32_t timer_high;
static volatile uint8_t sampling;
static uint8_t ready_mask, boot_pending;
static volatile uint64_t ttl_start;
static volatile uint8_t ttl_start_known;
static uint32_t last_meta, last_status, tx_errors, max_read_delay_us;
static uint64_t last_samples_sent;
static uint8_t active_sclk = 0xFFU;

uint64_t DualADC_TimeUs(void)
{
  uint32_t primask = __get_PRIMASK(); __disable_irq();
  uint32_t high = timer_high, low = TIM2->CNT;
  if ((TIM2->SR & TIM_SR_UIF) && low < 0x80000000U) ++high;
  __set_PRIMASK(primask);
  return ((uint64_t)high << 32U) | low;
}

static void delay_us(uint32_t us)
{
  uint64_t begin = DualADC_TimeUs();
  while (DualADC_TimeUs() - begin < us) { }
}

static uint8_t transfer(const uint8_t *in, uint8_t *out, uint16_t length, uint8_t commands)
{
  uint64_t begin = DualADC_TimeUs();
  __HAL_SPI_ENABLE(&hspi1);
  for (uint16_t i = 0U; i < length; ++i)
  {
    while (!(SPI1->SR & SPI_SR_TXE)) if (DualADC_TimeUs() - begin > 500U) return 0U;
    *(__IO uint8_t *)&SPI1->DR = in[i];
    while (!(SPI1->SR & SPI_SR_RXNE)) if (DualADC_TimeUs() - begin > 500U) return 0U;
    out[i] = *(__IO uint8_t *)&SPI1->DR;
    if (commands) delay_us(10U);
  }
  while (SPI1->SR & SPI_SR_BSY) if (DualADC_TimeUs() - begin > 500U) return 0U;
  return 1U;
}

/* ADS1292R raises DRDY on SCLK even while deselected. Keep the other
 * converter's SCLK quiet so its asynchronous DRDY cannot be shortened by
 * this transfer. PA5 and PB3 are both SPI1_SCK AF5; only one is in AF mode.
 * Called with both CS high and the previous transfer already complete. */
static void route_sclk(uint8_t device)
{
  if (active_sclk == device) return;
  GPIO_InitTypeDef g = {0};
  __HAL_SPI_DISABLE(&hspi1);
  HAL_GPIO_WritePin(GPIOA, GPIO_PIN_5, GPIO_PIN_RESET);
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_3, GPIO_PIN_RESET);
  g.Mode = GPIO_MODE_OUTPUT_PP; g.Pull = GPIO_NOPULL; g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  g.Pin = GPIO_PIN_5; HAL_GPIO_Init(GPIOA, &g);
  g.Pin = GPIO_PIN_3; HAL_GPIO_Init(GPIOB, &g);
  g.Pin = device == 0U ? GPIO_PIN_5 : GPIO_PIN_3;
  g.Mode = GPIO_MODE_AF_PP; g.Alternate = GPIO_AF5_SPI1;
  HAL_GPIO_Init(device == 0U ? GPIOA : GPIOB, &g);
  active_sclk = device;
  /* transfer() enables SPI after the selected CS is asserted. */
}

static uint8_t transaction(uint8_t d, const uint8_t *in, uint8_t *out, uint16_t len, uint8_t commands)
{
  route_sclk(d);
  HAL_GPIO_WritePin(dev[d].cs_port, dev[d].cs_pin, GPIO_PIN_RESET);
  uint8_t ok = transfer(in, out, len, commands);
  delay_us(10U); /* Covers CS hold even at the slow default ADC clock. */
  HAL_GPIO_WritePin(dev[d].cs_port, dev[d].cs_pin, GPIO_PIN_SET);
  if (!ok)
  {
    __HAL_SPI_DISABLE(&hspi1);
    __HAL_SPI_CLEAR_OVRFLAG(&hspi1);
    __HAL_SPI_ENABLE(&hspi1);
  }
  return ok;
}

static uint8_t command(uint8_t d, uint8_t op)
{
  uint8_t rx;
  uint8_t ok = transaction(d, &op, &rx, 1U, 1U);
  delay_us(20U);
  return ok;
}

static uint8_t init_device(uint8_t d)
{
  uint8_t txbuf[14] = {0}, rx[14] = {0};
  if (!command(d, 0x11U) || !command(d, 0x0AU) || !command(d, 0x06U)) return 1U;
  HAL_Delay(10U);
  if (!command(d, 0x11U)) return 2U;
  txbuf[0] = 0x41U; txbuf[1] = 10U;
  const uint8_t config[11] = {3U,0xA0U,0x10U,0U,0U,0U,0U,0x40U,2U,3U,0x0CU};
  memcpy(txbuf + 2, config, sizeof(config));
  txbuf[7] = d == 0U ? 0x2FU : 0U; /* One RLD loop; B is disabled. */
  if (!transaction(d, txbuf, rx, 13U, 1U)) return 3U;
  delay_us(20U);
  memset(txbuf, 0, sizeof(txbuf)); txbuf[0] = 0x20U; txbuf[1] = 11U;
  if (!transaction(d, txbuf, rx, 14U, 1U)) return 4U;
  memcpy(dev[d].registers, rx + 2, 12U);
  if (dev[d].registers[0] != 0x53U && dev[d].registers[0] != 0x73U) return 5U;
  if (dev[d].registers[1] != 3U || dev[d].registers[4] != 0U || dev[d].registers[5] != 0U ||
      dev[d].registers[6] != (d == 0U ? 0x2FU : 0U) || !(dev[d].registers[8] & 0x40U)) return 6U;
  return 0U;
}

static void timestamp_init(void)
{
  GPIO_InitTypeDef g = {0};
  __HAL_RCC_TIM2_CLK_ENABLE();
  g.Pin = NM_DRDY_PIN_MASK; g.Mode = GPIO_MODE_AF_PP; g.Pull = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_HIGH; g.Alternate = GPIO_AF1_TIM2;
#if NM_DRDY_PORT_B
  HAL_GPIO_Init(GPIOB, &g); /* PB8=TIM2_CH1, PB2=TIM2_CH4 (AF1). */
#else
  HAL_GPIO_Init(GPIOA, &g); /* PA0=TIM2_CH1, PA1=TIM2_CH2 (AF1). */
#endif
  g.Pin = GPIO_PIN_10; HAL_GPIO_Init(GPIOB, &g);
  HAL_NVIC_DisableIRQ(EXTI9_5_IRQn);
  TIM2->CR1 = 0U; TIM2->PSC = 83U; TIM2->ARR = 0xFFFFFFFFU;
#ifdef NEUROMOTION_RIGHT_DRDY
  TIM2->CCMR1 = TIM_CCMR1_CC1S_0;
#else
  TIM2->CCMR1 = TIM_CCMR1_CC1S_0 | TIM_CCMR1_CC2S_0;
#endif
  /* TTL only: fDTS/32, 8 stable samples (~3 us at 84 MHz). DRDY filters
   * remain unchanged. This rejects sub-microsecond input chatter without
   * changing the 500 ms TTL symbols. ST LL_TIM_IC_FILTER_FDIV32_N8. */
  TIM2->CCMR2 = TIM_CCMR2_CC3S_0 | TIM_CCMR2_IC3F;
#ifdef NEUROMOTION_RIGHT_DRDY
  TIM2->CCMR2 |= TIM_CCMR2_CC4S_0;
  TIM2->CCER = TIM_CCER_CC1E | TIM_CCER_CC1P | TIM_CCER_CC4E | TIM_CCER_CC4P |
               TIM_CCER_CC3E | TIM_CCER_CC3P | TIM_CCER_CC3NP;
#else
  TIM2->CCER = TIM_CCER_CC1E | TIM_CCER_CC1P | TIM_CCER_CC2E | TIM_CCER_CC2P |
               TIM_CCER_CC3E | TIM_CCER_CC3P | TIM_CCER_CC3NP;
#endif
  TIM2->EGR = TIM_EGR_UG; TIM2->SR = 0U;
  TIM2->DIER = TIM_DIER_UIE | NM_DRDY_A_IF | NM_DRDY_B_IF | TIM_DIER_CC3IE;
  HAL_NVIC_SetPriority(TIM2_IRQn, 1U, 0U); HAL_NVIC_EnableIRQ(TIM2_IRQn);
  TIM2->CR1 = TIM_CR1_CEN;
}

static uint64_t extend_capture(uint32_t value, uint32_t status)
{
  uint32_t high = timer_high;
  if ((status & TIM_SR_UIF) && value < 0x80000000U) ++high;
  return ((uint64_t)high << 32U) | value;
}

void DualADC_TimerIRQ(void)
{
  uint32_t sr = TIM2->SR;
  for (uint8_t d = 0U; d < 2U; ++d)
  {
    uint32_t flag = d == 0U ? NM_DRDY_A_IF : NM_DRDY_B_IF;
    uint32_t over = d == 0U ? NM_DRDY_A_OF : NM_DRDY_B_OF;
    if (sr & flag)
    {
#if NM_DRDY_B_CHANNEL == 4U
      uint32_t value = d == 0U ? TIM2->CCR1 : TIM2->CCR4;
#else
      uint32_t value = d == 0U ? TIM2->CCR1 : TIM2->CCR2;
#endif
      if (!sampling) continue;
      uint8_t fault = 0U;
      if ((sr | TIM2->SR) & over)
      {
        ++dev[d].overcapture; dev[d].index = 0U; fault = NM_V2_CAPTURE_ERROR;
      }
      uint32_t n = dev[d].index++; dev[d].latest = n;
      if (cap_head - cap_tail >= CAPACITY) { ++dev[d].dropped; continue; }
      Capture *c = &captures[cap_head % CAPACITY];
      c->time = extend_capture(value, sr); c->index = n; c->device = d; c->fault = fault;
      __DMB(); ++cap_head;
    }
  }
  if (sr & TIM_SR_CC3IF)
  {
    uint32_t value = TIM2->CCR3;
    uint8_t level = (GPIOB->IDR & GPIO_PIN_10) ? 1U : 0U;
    uint64_t time = extend_capture(value, sr);
    uint32_t n = edge_index++;
    if (n == 0U && level) { ttl_start = time; ttl_start_known = 1U; }
    if (edge_head - edge_tail < EDGES)
    {
      TTLEdge *e = &edges[edge_head % EDGES]; e->time = time; e->index = n; e->level = level;
      __DMB(); ++edge_head;
    }
    else { ++edge_dropped; ++edge_queue_dropped; }
    if (sr & TIM_SR_CC3OF) { ++edge_dropped; ++edge_overcapture; }
  }
  if (sr & TIM_SR_UIF) ++timer_high;
  TIM2->SR = ~(sr & (TIM_SR_UIF | NM_DRDY_SR_MASK | TIM_SR_CC3IF | TIM_SR_CC3OF));
}

static void uart_dma_init(void)
{
  __HAL_RCC_DMA1_CLK_ENABLE();
  dma_tx.Instance = DMA1_Stream6; dma_tx.Init.Channel = DMA_CHANNEL_4;
  dma_tx.Init.Direction = DMA_MEMORY_TO_PERIPH; dma_tx.Init.PeriphInc = DMA_PINC_DISABLE;
  dma_tx.Init.MemInc = DMA_MINC_ENABLE; dma_tx.Init.PeriphDataAlignment = DMA_PDATAALIGN_BYTE;
  dma_tx.Init.MemDataAlignment = DMA_MDATAALIGN_BYTE; dma_tx.Init.Mode = DMA_NORMAL;
  dma_tx.Init.Priority = DMA_PRIORITY_MEDIUM; dma_tx.Init.FIFOMode = DMA_FIFOMODE_DISABLE;
  if (HAL_DMA_Init(&dma_tx) != HAL_OK) Error_Handler();
  __HAL_LINKDMA(&huart2, hdmatx, dma_tx);
  HAL_NVIC_SetPriority(DMA1_Stream6_IRQn, 3U, 0U); HAL_NVIC_EnableIRQ(DMA1_Stream6_IRQn);
  HAL_NVIC_SetPriority(USART2_IRQn, 3U, 0U); HAL_NVIC_EnableIRQ(USART2_IRQn);
}
void DualADC_DMAIRQ(void) { HAL_DMA_IRQHandler(&dma_tx); }
void DualADC_UARTIRQ(void) { HAL_UART_IRQHandler(&huart2); }

uint8_t DualADC_Init(void)
{
  GPIO_InitTypeDef g = {0};
  dev[0].cs_port = GPIOB; dev[0].cs_pin = GPIO_PIN_9; dev[0].pwdn_pin = GPIO_PIN_8;
  dev[1].cs_port = GPIOB; dev[1].cs_pin = GPIO_PIN_6; dev[1].pwdn_pin = GPIO_PIN_9;
  HAL_GPIO_WritePin(GPIOB, GPIO_PIN_9 | GPIO_PIN_6, GPIO_PIN_SET);
  g.Pin = GPIO_PIN_9 | GPIO_PIN_6; g.Mode = GPIO_MODE_OUTPUT_PP; g.Pull = GPIO_NOPULL;
  g.Speed = GPIO_SPEED_FREQ_HIGH; HAL_GPIO_Init(GPIOB, &g);
  HAL_GPIO_WritePin(GPIOC, GPIO_PIN_8 | GPIO_PIN_9, GPIO_PIN_RESET);
  g.Pin = GPIO_PIN_8 | GPIO_PIN_9; HAL_GPIO_Init(GPIOC, &g);
  HAL_GPIO_WritePin(ADS1292_START_GPIO_Port, ADS1292_START_Pin, GPIO_PIN_RESET);
  route_sclk(0U);
  timestamp_init(); uart_dma_init();
  HAL_Delay(50U); HAL_GPIO_WritePin(GPIOC, GPIO_PIN_8 | GPIO_PIN_9, GPIO_PIN_SET); HAL_Delay(100U);
  for (uint8_t d = 0U; d < 2U; ++d)
  {
    dev[d].error = init_device(d);
    if (dev[d].error == 0U) ready_mask |= (uint8_t)(1U << d);
  }
  boot_pending = 1U; last_meta = last_status = HAL_GetTick();
  DualADC_Process();
  return ready_mask;
}

void DualADC_Start(void)
{
  if (ready_mask != 3U) return;
  if (!command(0U, 0x10U) || !command(1U, 0x10U)) { ++tx_errors; return; }
  sampling = 1U;
  HAL_GPIO_WritePin(ADS1292_START_GPIO_Port, ADS1292_START_Pin, GPIO_PIN_SET);
}

static int32_t code24(const uint8_t *p)
{
  uint32_t x = ((uint32_t)p[0] << 16U) | ((uint32_t)p[1] << 8U) | p[2];
  return (int32_t)(x & 0x800000U ? x | 0xFF000000U : x);
}

static void read_capture(Capture c)
{
  Device *d = &dev[c.device]; uint8_t rx[9] = {0}, dummy[9] = {0};
  uint64_t age = DualADC_TimeUs() - c.time;
  uint8_t flags = c.fault, ok = 0U; uint32_t status = 0U;
  if (age > max_read_delay_us) max_read_delay_us = age > 0xFFFFFFFFU ? 0xFFFFFFFFU : (uint32_t)age;
  if (c.index != d->latest || age > 750U) { flags |= NM_V2_READ_LATE; ++d->late; }
  else if (!transaction(c.device, dummy, rx, 9U, 0U)) { flags |= NM_V2_SPI_ERROR; ++d->spi_errors; }
  else
  {
    status = ((uint32_t)rx[0] << 16U) | ((uint32_t)rx[1] << 8U) | rx[2];
    if ((status >> 20U) != 12U) { flags |= NM_V2_STATUS_ERROR; ++d->invalid_status; }
    else if (c.index != d->latest || DualADC_TimeUs() - c.time >= 950U) { flags |= NM_V2_READ_LATE; ++d->late; }
    else if (!flags) { flags = NM_V2_VALID; ok = 1U; }
  }
  if (!ok) ++d->dropped;
  if (rec_head - rec_tail >= RECORDS) { ++d->dropped; ++record_dropped; return; }
  NM_V2_Record(records[rec_head % RECORDS], c.device | flags, status, c.index, c.time,
               code24(rx + 3), code24(rx + 6), d->dropped);
  ++rec_head;
}

static uint8_t send(uint8_t kind, uint16_t length)
{
  size_t size = NM_V2_Frame(tx, kind, packet_sequence, payload, length);
  if (!size || HAL_UART_Transmit_DMA(&huart2, tx, (uint16_t)size) != HAL_OK) { ++tx_errors; return 0U; }
  ++packet_sequence; return 1U;
}

static void metadata(void)
{
  char regs[2][25];
  for (uint8_t d = 0U; d < 2U; ++d)
    for (uint8_t j = 0U; j < 12U; ++j) (void)snprintf(&regs[d][j * 2U], 3U, "%02x", dev[d].registers[j]);
  char anchor[28];
  if (ttl_start_known) (void)snprintf(anchor, sizeof(anchor), "%llu", (unsigned long long)ttl_start);
  else (void)snprintf(anchor, sizeof(anchor), "null");
  int n = snprintf((char *)payload, sizeof(payload),
      "{\"firmware\":\"" NM_FIRMWARE_NAME "\",\"channel_count\":4,\"sample_rate_hz\":1000,\"tick_hz\":1000000,"
      "\"vref_uv\":2420000,\"gains\":[6,6,6,6],\"ready_mask\":%u,\"id_a\":%u,\"id_b\":%u,"
      "\"error_a\":%u,\"error_b\":%u,\"registers_a\":\"%s\",\"registers_b\":\"%s\",\"ttl_start_us\":%s,"
      "\"sclk_a\":\"PA5\",\"sclk_b\":\"PB3\",\"sclk_routing\":\"selected_only\","
      NM_DRDY_METADATA
      "\"ttl_capture_filter\":\"DTS_div32_N8\"}",
      ready_mask, dev[0].registers[0], dev[1].registers[0], dev[0].error, dev[1].error, regs[0], regs[1], anchor);
  if (n > 0 && n < (int)sizeof(payload) && send(boot_pending ? NM_V2_BOOT : NM_V2_META, (uint16_t)n))
  { boot_pending = 0U; last_meta = HAL_GetTick(); }
}

void DualADC_Process(void)
{
  for (uint8_t k = 0U; k < 8U && cap_tail != cap_head; ++k)
  {
    __DMB(); Capture c = captures[cap_tail % CAPACITY]; ++cap_tail; read_capture(c);
  }
  if (huart2.gState != HAL_UART_STATE_READY) return;
  if (boot_pending || HAL_GetTick() - last_meta >= 1000U) { metadata(); return; }
  if (edge_tail != edge_head)
  {
    __DMB(); TTLEdge e = edges[edge_tail % EDGES];
    memset(payload, 0, 16U); NM_Store32(payload, e.index); NM_Store64(payload + 4, e.time); payload[12] = e.level;
    if (send(NM_V2_SYNC, 16U)) ++edge_tail;
    return;
  }
  if (HAL_GetTick() - last_status >= 1000U)
  {
    int n = snprintf((char *)payload, sizeof(payload),
        "{\"capture_overrun_a\":%lu,\"capture_overrun_b\":%lu,\"dropped_a\":%lu,\"dropped_b\":%lu,"
        "\"spi_errors_a\":%lu,\"spi_errors_b\":%lu,\"late_a\":%lu,\"late_b\":%lu,"
        "\"status_errors_a\":%lu,\"status_errors_b\":%lu,\"edge_dropped\":%lu,\"record_dropped\":%lu,"
        "\"tx_errors\":%lu,\"max_read_delay_us\":%lu,\"edge_overcapture\":%lu,\"edge_queue_dropped\":%lu}",
        (unsigned long)dev[0].overcapture,(unsigned long)dev[1].overcapture,
        (unsigned long)dev[0].dropped,(unsigned long)dev[1].dropped,
        (unsigned long)dev[0].spi_errors,(unsigned long)dev[1].spi_errors,
        (unsigned long)dev[0].late,(unsigned long)dev[1].late,
        (unsigned long)dev[0].invalid_status,(unsigned long)dev[1].invalid_status,
        (unsigned long)edge_dropped,(unsigned long)record_dropped,(unsigned long)tx_errors,(unsigned long)max_read_delay_us,
        (unsigned long)edge_overcapture,(unsigned long)edge_queue_dropped);
    if (n > 0 && n < (int)sizeof(payload) && send(NM_V2_STATUS, (uint16_t)n)) last_status = HAL_GetTick();
    return;
  }
  uint32_t count = rec_head - rec_tail;
  if (count && (count >= 8U || DualADC_TimeUs() - last_samples_sent >= 10000U))
  {
    if (count > 8U) count = 8U;
    for (uint32_t i = 0U; i < count; ++i) memcpy(payload + i * NM_V2_RECORD_SIZE, records[(rec_tail + i) % RECORDS], NM_V2_RECORD_SIZE);
    if (send(NM_V2_SAMPLES, (uint16_t)(count * NM_V2_RECORD_SIZE)))
    { rec_tail += count; last_samples_sent = DualADC_TimeUs(); }
  }
}
#endif
