#ifndef __ADS1292_H__
#define __ADS1292_H__

#ifdef __cplusplus
extern "C" {
#endif

#include "main.h"
#include <stdint.h>

typedef struct
{
  float ch1;
  float ch2;
  uint8_t sync;
} ADS1292_Sample_t;

#define ADS1292_STREAM_BUFFER_SIZE       500U
#define ADS1292_STREAM_RECORD_SIZE       20U
#define ADS1292_STREAM_PAYLOAD_SIZE      240U

HAL_StatusTypeDef ADS1292_Init(void);
HAL_StatusTypeDef ADS1292_Start(void);
void ADS1292_Stop(void);
void ADS1292_DRDY_Callback(uint16_t GPIO_Pin);
uint8_t ADS1292_ReadDeviceId(void);
uint8_t ADS1292_GetLastError(void);
uint32_t ADS1292_GetDroppedSamples(void);
uint8_t ADS1292_PopSample(ADS1292_Sample_t *sample);
uint16_t ADS1292_GetStreamByteCount(void);
uint8_t ADS1292_ReadStreamBytes(uint8_t *data, uint16_t len);

#ifdef __cplusplus
}
#endif

#endif /* __ADS1292_H__ */
