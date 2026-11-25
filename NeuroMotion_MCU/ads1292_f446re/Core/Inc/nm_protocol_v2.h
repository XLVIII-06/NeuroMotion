#ifndef NM_PROTOCOL_V2_H
#define NM_PROTOCOL_V2_H
#include <stdint.h>
#include <stddef.h>

#define NM_V2_META 1U
#define NM_V2_SAMPLES 2U
#define NM_V2_SYNC 3U
#define NM_V2_STATUS 4U
#define NM_V2_BOOT 5U
#define NM_V2_VALID 4U
#define NM_V2_SPI_ERROR 8U
#define NM_V2_STATUS_ERROR 16U
#define NM_V2_READ_LATE 32U
#define NM_V2_CAPTURE_ERROR 64U
#define NM_V2_MAX_PAYLOAD 1024U
#define NM_V2_RECORD_SIZE 28U

uint32_t NM_CRC32(const uint8_t *data, size_t length);
void NM_Store32(uint8_t *out, uint32_t value);
void NM_Store64(uint8_t *out, uint64_t value);
size_t NM_V2_Frame(uint8_t *out, uint8_t kind, uint32_t sequence, const uint8_t *payload, uint16_t length);
void NM_V2_Record(uint8_t *out, uint8_t source_flags, uint32_t status, uint32_t index,
                  uint64_t time_us, int32_t ch1, int32_t ch2, uint32_t dropped);
#endif
