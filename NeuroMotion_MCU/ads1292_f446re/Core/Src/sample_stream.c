#include "sample_stream.h"

#include "ads1292.h"
#include "usart.h"
#include <stdint.h>

#define SAMPLE_STREAM_FRAME_HEADER       0xA5U
#define SAMPLE_STREAM_FRAME_END          0x5AU
#define SAMPLE_STREAM_SAMPLE_ADDR        0x11U
#define SAMPLE_STREAM_FRAME_OVERHEAD     5U
#define SAMPLE_STREAM_FRAME_SIZE         (ADS1292_STREAM_PAYLOAD_SIZE + SAMPLE_STREAM_FRAME_OVERHEAD)
#define SAMPLE_STREAM_LENGTH_FIELD       (ADS1292_STREAM_PAYLOAD_SIZE + 2U)

static uint8_t stream_frame[SAMPLE_STREAM_FRAME_SIZE];

void SampleStream_Process(void)
{
  if (ADS1292_GetStreamByteCount() < ADS1292_STREAM_PAYLOAD_SIZE)
  {
    return;
  }

  stream_frame[0] = SAMPLE_STREAM_FRAME_HEADER;
  stream_frame[1] = (uint8_t)SAMPLE_STREAM_LENGTH_FIELD;
  stream_frame[2] = SAMPLE_STREAM_SAMPLE_ADDR;
  stream_frame[3] = (uint8_t)(stream_frame[1] ^ stream_frame[2]);

  if (ADS1292_ReadStreamBytes(&stream_frame[4], ADS1292_STREAM_PAYLOAD_SIZE) == 0U)
  {
    return;
  }

  stream_frame[SAMPLE_STREAM_FRAME_SIZE - 1U] = SAMPLE_STREAM_FRAME_END;
  (void)HAL_UART_Transmit(&huart2, stream_frame, SAMPLE_STREAM_FRAME_SIZE, 0xFFFFU);
}
