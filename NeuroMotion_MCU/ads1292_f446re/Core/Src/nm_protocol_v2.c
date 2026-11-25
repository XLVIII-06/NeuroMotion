#include "nm_protocol_v2.h"
#include <string.h>

uint32_t NM_CRC32(const uint8_t *data, size_t length)
{
  uint32_t crc = 0xFFFFFFFFU;
  for (size_t i = 0U; i < length; ++i)
  {
    crc ^= data[i];
    for (uint8_t bit = 0U; bit < 8U; ++bit)
      crc = (crc >> 1U) ^ (0xEDB88320U & (0U - (crc & 1U)));
  }
  return crc ^ 0xFFFFFFFFU;
}

void NM_Store32(uint8_t *out, uint32_t value)
{
  for (uint8_t i = 0U; i < 4U; ++i) out[i] = (uint8_t)(value >> (8U * i));
}

void NM_Store64(uint8_t *out, uint64_t value)
{
  NM_Store32(out, (uint32_t)value);
  NM_Store32(out + 4, (uint32_t)(value >> 32U));
}

size_t NM_V2_Frame(uint8_t *out, uint8_t kind, uint32_t sequence, const uint8_t *payload, uint16_t length)
{
  if (length > NM_V2_MAX_PAYLOAD) return 0U;
  memcpy(out, "NM2!", 4U);
  out[4] = 2U; out[5] = kind; out[6] = (uint8_t)length; out[7] = (uint8_t)(length >> 8U);
  NM_Store32(out + 8, sequence);
  if (length) memcpy(out + 12, payload, length);
  NM_Store32(out + 12 + length, NM_CRC32(out, 12U + length));
  return 16U + length;
}

void NM_V2_Record(uint8_t *out, uint8_t source_flags, uint32_t status, uint32_t index,
                  uint64_t time_us, int32_t ch1, int32_t ch2, uint32_t dropped)
{
  out[0] = source_flags;
  out[1] = (uint8_t)status; out[2] = (uint8_t)(status >> 8U); out[3] = (uint8_t)(status >> 16U);
  NM_Store32(out + 4, index); NM_Store64(out + 8, time_us);
  NM_Store32(out + 16, (uint32_t)ch1); NM_Store32(out + 20, (uint32_t)ch2);
  NM_Store32(out + 24, dropped);
}
