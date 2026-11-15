#include "ads1292.h"

#include "spi.h"
#include "sync_signal.h"
#include <string.h>

#define ADS1292_CMD_WAKEUP      0x02U
#define ADS1292_CMD_RESET       0x06U
#define ADS1292_CMD_START       0x08U
#define ADS1292_CMD_STOP        0x0AU
#define ADS1292_CMD_RDATAC      0x10U
#define ADS1292_CMD_SDATAC      0x11U
#define ADS1292_CMD_RREG        0x20U
#define ADS1292_CMD_WREG        0x40U

#define ADS1292_REG_ID          0x00U
#define ADS1292_REG_CONFIG1     0x01U
#define ADS1292_REG_CONFIG2     0x02U
#define ADS1292_REG_LOFF        0x03U
#define ADS1292_REG_CH1SET      0x04U
#define ADS1292_REG_CH2SET      0x05U
#define ADS1292_REG_RLD_SENS    0x06U
#define ADS1292_REG_LOFF_SENS   0x07U
#define ADS1292_REG_LOFF_STAT   0x08U
#define ADS1292_REG_RESP1       0x09U
#define ADS1292_REG_RESP2       0x0AU
#define ADS1292_REG_GPIO        0x0BU

#define ADS1292_VREF_V          2.42f
#define ADS1292_PGA_GAIN        6.0f
#define ADS1292_UV_PER_CODE     ((2.0f * ADS1292_VREF_V * 1000000.0f) / 16777216.0f / ADS1292_PGA_GAIN)

#define ADS1292_CS_HIGH()       HAL_GPIO_WritePin(ADS_CS_GPIO_Port, ADS_CS_Pin, GPIO_PIN_SET)
#define ADS1292_CS_LOW()        HAL_GPIO_WritePin(ADS_CS_GPIO_Port, ADS_CS_Pin, GPIO_PIN_RESET)
#define ADS1292_START_HIGH()    HAL_GPIO_WritePin(ADS1292_START_GPIO_Port, ADS1292_START_Pin, GPIO_PIN_SET)
#define ADS1292_START_LOW()     HAL_GPIO_WritePin(ADS1292_START_GPIO_Port, ADS1292_START_Pin, GPIO_PIN_RESET)
#define ADS1292_PWDN_HIGH()     HAL_GPIO_WritePin(ADS1292_PWDN_GPIO_Port, ADS1292_PWDN_Pin, GPIO_PIN_SET)
#define ADS1292_PWDN_LOW()      HAL_GPIO_WritePin(ADS1292_PWDN_GPIO_Port, ADS1292_PWDN_Pin, GPIO_PIN_RESET)

static uint8_t stream_buffer[ADS1292_STREAM_BUFFER_SIZE];
static volatile uint16_t stream_head;
static volatile uint16_t stream_tail;
static volatile uint16_t stream_count;
static volatile uint32_t dropped_samples;
static volatile uint32_t sample_index;
static volatile uint8_t sampling_enabled;

static uint8_t ads_regs[12];
static uint8_t device_id;
static uint8_t last_error;

static void ADS1292_DelayCommand(void)
{
  uint32_t start = HAL_GetTick();
  while ((HAL_GetTick() - start) < 1U)
  {
  }
}

static HAL_StatusTypeDef ADS1292_SPI_TransmitReceive(uint8_t *tx, uint8_t *rx, uint16_t len)
{
  return HAL_SPI_TransmitReceive(&hspi1, tx, rx, len, 10U);
}

static HAL_StatusTypeDef ADS1292_SendCommand(uint8_t command)
{
  HAL_StatusTypeDef status;
  uint8_t rx = 0U;

  ADS1292_CS_LOW();
  status = ADS1292_SPI_TransmitReceive(&command, &rx, 1U);
  ADS1292_CS_HIGH();
  ADS1292_DelayCommand();

  return status;
}

static HAL_StatusTypeDef ADS1292_WriteRegisters(uint8_t address, const uint8_t *values, uint8_t length)
{
  HAL_StatusTypeDef status;
  uint8_t command[2];
  uint8_t dummy[12] = {0};
  uint8_t tx_values[12];

  if ((length == 0U) || (length > sizeof(tx_values)))
  {
    return HAL_ERROR;
  }

  for (uint8_t i = 0U; i < length; i++)
  {
    tx_values[i] = values[i];
  }

  command[0] = ADS1292_CMD_WREG | (address & 0x1FU);
  command[1] = (uint8_t)(length - 1U);

  ADS1292_CS_LOW();
  status = ADS1292_SPI_TransmitReceive(command, dummy, sizeof(command));
  if (status == HAL_OK)
  {
    status = ADS1292_SPI_TransmitReceive(tx_values, dummy, length);
  }
  ADS1292_CS_HIGH();
  ADS1292_DelayCommand();

  return status;
}

static HAL_StatusTypeDef ADS1292_ReadRegisters(uint8_t address, uint8_t *values, uint8_t length)
{
  HAL_StatusTypeDef status;
  uint8_t command[2];
  uint8_t dummy[12] = {0};

  if ((length == 0U) || (length > sizeof(dummy)))
  {
    return HAL_ERROR;
  }

  command[0] = ADS1292_CMD_RREG | (address & 0x1FU);
  command[1] = (uint8_t)(length - 1U);

  ADS1292_CS_LOW();
  status = ADS1292_SPI_TransmitReceive(command, dummy, sizeof(command));
  if (status == HAL_OK)
  {
    status = ADS1292_SPI_TransmitReceive(dummy, values, length);
  }
  ADS1292_CS_HIGH();
  ADS1292_DelayCommand();

  return status;
}

static int32_t ADS1292_SignExtend24(const uint8_t *data)
{
  int32_t value = ((int32_t)data[0] << 16) | ((int32_t)data[1] << 8) | (int32_t)data[2];

  if ((value & 0x00800000L) != 0)
  {
    value |= 0xFF000000L;
  }

  return value;
}

static float ADS1292_CodeToMicrovolts(int32_t code)
{
  return (float)code * ADS1292_UV_PER_CODE;
}

static void ADS1292_WriteLe32(uint8_t *dest, uint32_t value)
{
  dest[0] = (uint8_t)(value & 0xFFU);
  dest[1] = (uint8_t)((value >> 8) & 0xFFU);
  dest[2] = (uint8_t)((value >> 16) & 0xFFU);
  dest[3] = (uint8_t)((value >> 24) & 0xFFU);
}

static void ADS1292_WriteFloatLe(uint8_t *dest, float value)
{
  uint32_t bits;
  memcpy(&bits, &value, sizeof(bits));
  ADS1292_WriteLe32(dest, bits);
}

static void ADS1292_PushRecord(float ch1, float ch2, uint8_t sync)
{
  uint8_t record[ADS1292_STREAM_RECORD_SIZE];
  uint32_t current_index = sample_index++;
  uint32_t dropped_snapshot = dropped_samples;

  ADS1292_WriteLe32(&record[0], current_index);
  ADS1292_WriteFloatLe(&record[4], ch1);
  ADS1292_WriteFloatLe(&record[8], ch2);
  record[12] = (sync != 0U) ? 1U : 0U;
  record[13] = 0U;
  record[14] = 0U;
  record[15] = 0U;
  ADS1292_WriteLe32(&record[16], dropped_snapshot);

  if (ADS1292_STREAM_RECORD_SIZE > (ADS1292_STREAM_BUFFER_SIZE - stream_count))
  {
    dropped_samples++;
    return;
  }

  for (uint16_t i = 0U; i < ADS1292_STREAM_RECORD_SIZE; i++)
  {
    stream_buffer[stream_head] = record[i];
    stream_head = (uint16_t)((stream_head + 1U) % ADS1292_STREAM_BUFFER_SIZE);
  }

  stream_count = (uint16_t)(stream_count + ADS1292_STREAM_RECORD_SIZE);
}

HAL_StatusTypeDef ADS1292_Init(void)
{
  HAL_StatusTypeDef status;
  uint8_t config[11];

  last_error = 0U;
  sampling_enabled = 0U;
  stream_head = 0U;
  stream_tail = 0U;
  stream_count = 0U;
  dropped_samples = 0U;
  sample_index = 0U;

  ADS1292_CS_HIGH();
  ADS1292_START_LOW();
  ADS1292_PWDN_LOW();
  HAL_Delay(50U);
  ADS1292_PWDN_HIGH();
  HAL_Delay(100U);

  status = ADS1292_SendCommand(ADS1292_CMD_SDATAC);
  if (status != HAL_OK)
  {
    last_error = 1U;
    return status;
  }

  status = ADS1292_SendCommand(ADS1292_CMD_STOP);
  if (status != HAL_OK)
  {
    last_error = 2U;
    return status;
  }

  status = ADS1292_SendCommand(ADS1292_CMD_RESET);
  if (status != HAL_OK)
  {
    last_error = 3U;
    return status;
  }
  HAL_Delay(10U);

  status = ADS1292_SendCommand(ADS1292_CMD_SDATAC);
  if (status != HAL_OK)
  {
    last_error = 4U;
    return status;
  }

  config[ADS1292_REG_CONFIG1 - 1U] = 0x03U; /* 1 kSPS, continuous conversion. */
  config[ADS1292_REG_CONFIG2 - 1U] = 0xA0U; /* Internal 2.42-V reference buffer, test signal off. */
  config[ADS1292_REG_LOFF - 1U] = 0x10U;
  config[ADS1292_REG_CH1SET - 1U] = 0x00U;  /* PGA = 6, normal electrode input. */
  config[ADS1292_REG_CH2SET - 1U] = 0x00U;  /* PGA = 6, normal electrode input. */
  config[ADS1292_REG_RLD_SENS - 1U] = 0x2FU; /* Enable RLD and derive its common-mode signal from both channels. */
  config[ADS1292_REG_LOFF_SENS - 1U] = 0x00U;
  config[ADS1292_REG_LOFF_STAT - 1U] = 0x40U; /* CLK_DIV = 1 for 2.048-MHz external clock. */
  config[ADS1292_REG_RESP1 - 1U] = 0x02U;
  config[ADS1292_REG_RESP2 - 1U] = 0x03U;
  config[ADS1292_REG_GPIO - 1U] = 0x0CU;

  status = ADS1292_WriteRegisters(ADS1292_REG_CONFIG1, config, sizeof(config));
  if (status != HAL_OK)
  {
    last_error = 5U;
    return status;
  }

  status = ADS1292_ReadRegisters(ADS1292_REG_ID, ads_regs, sizeof(ads_regs));
  if (status != HAL_OK)
  {
    last_error = 6U;
    return status;
  }

  device_id = ads_regs[ADS1292_REG_ID];
  if ((device_id != 0x53U) && (device_id != 0x73U))
  {
    last_error = 7U;
    return HAL_ERROR;
  }

  if ((ads_regs[ADS1292_REG_CONFIG1] != 0x03U) ||
      (ads_regs[ADS1292_REG_CH1SET] != 0x00U) ||
      (ads_regs[ADS1292_REG_CH2SET] != 0x00U) ||
      (ads_regs[ADS1292_REG_RLD_SENS] != 0x2FU) ||
      ((ads_regs[ADS1292_REG_RESP2] & 0x02U) == 0U) ||
      ((ads_regs[ADS1292_REG_LOFF_STAT] & 0x40U) == 0U))
  {
    last_error = 8U;
    return HAL_ERROR;
  }

  return HAL_OK;
}

HAL_StatusTypeDef ADS1292_Start(void)
{
  HAL_StatusTypeDef status;

  stream_head = 0U;
  stream_tail = 0U;
  stream_count = 0U;
  dropped_samples = 0U;
  sample_index = 0U;

  status = ADS1292_SendCommand(ADS1292_CMD_RDATAC);
  if (status != HAL_OK)
  {
    last_error = 9U;
    return status;
  }

  ADS1292_START_HIGH();
  ADS1292_CS_LOW();
  sampling_enabled = 1U;

  return HAL_OK;
}

void ADS1292_Stop(void)
{
  sampling_enabled = 0U;
  ADS1292_START_LOW();
  ADS1292_CS_HIGH();
  (void)ADS1292_SendCommand(ADS1292_CMD_STOP);
  (void)ADS1292_SendCommand(ADS1292_CMD_SDATAC);
}

void ADS1292_DRDY_Callback(uint16_t GPIO_Pin)
{
  uint8_t tx[9] = {0};
  uint8_t rx[9] = {0};
  int32_t raw_ch1;
  int32_t raw_ch2;
  float ch1;
  float ch2;

  if ((GPIO_Pin != ADS1292_DRDY_Pin) || (sampling_enabled == 0U))
  {
    return;
  }

  if (ADS1292_SPI_TransmitReceive(tx, rx, sizeof(rx)) != HAL_OK)
  {
    last_error = 10U;
    return;
  }

  raw_ch1 = ADS1292_SignExtend24(&rx[3]);
  raw_ch2 = ADS1292_SignExtend24(&rx[6]);
  ch1 = ADS1292_CodeToMicrovolts(raw_ch1);
  ch2 = ADS1292_CodeToMicrovolts(raw_ch2);
  ADS1292_PushRecord(ch1, ch2, SyncSignal_GetLevel());
}

uint8_t ADS1292_ReadDeviceId(void)
{
  return device_id;
}

uint8_t ADS1292_GetLastError(void)
{
  return last_error;
}

uint32_t ADS1292_GetDroppedSamples(void)
{
  return dropped_samples;
}

uint8_t ADS1292_PopSample(ADS1292_Sample_t *sample)
{
  (void)sample;
  return 0U;
}

uint16_t ADS1292_GetStreamByteCount(void)
{
  uint16_t count;

  __disable_irq();
  count = stream_count;
  __enable_irq();

  return count;
}

uint8_t ADS1292_ReadStreamBytes(uint8_t *data, uint16_t len)
{
  if ((data == NULL) || (len > ADS1292_STREAM_BUFFER_SIZE))
  {
    return 0U;
  }

  __disable_irq();
  if (len > stream_count)
  {
    __enable_irq();
    return 0U;
  }

  for (uint16_t i = 0U; i < len; i++)
  {
    data[i] = stream_buffer[stream_tail];
    stream_tail = (uint16_t)((stream_tail + 1U) % ADS1292_STREAM_BUFFER_SIZE);
  }

  stream_count = (uint16_t)(stream_count - len);
  __enable_irq();
  return 1U;
}
