# Four-channel native protocol v2

The legacy two-channel target retains its 245-byte v1 frames. Both `NeuroMotion_4CH` and `NeuroMotion_4CH_Right` use the following distinct protocol. UART remains 921600 baud, 8N1.

## Frame

All integers are little endian. Header: ASCII `NM2!` (4 bytes), version `2` (uint8), type (uint8), payload length (uint16), packet sequence (uint32). Payload is at most 1024 bytes, followed by CRC32/ISO-HDLC (uint32), covering header plus payload. This matches Python `zlib.crc32`; `123456789` gives `CBF43926`. Frame overhead is 16 bytes. Sequence wraps modulo 2^32; a BOOT starts a new MCU epoch.

Types: META=1, SAMPLES=2, SYNC_EDGE=3, STATUS=4, BOOT=5. JSON packets are UTF-8 objects, never NaN/Infinity. Unknown versions, invalid lengths, malformed objects and CRC failures are rejected before interpretation.

META/BOOT fields: `firmware`, `channel_count=4`, `sample_rate_hz=1000`, `tick_hz=1000000`, `vref_uv=2420000`, `gains=[6,6,6,6]`, `ready_mask`, device IDs, initialization errors, register readbacks and nullable `ttl_start_us`. Readiness 3 means both ADCs initialized. Metadata is repeated every second; missing metadata does not authorize guessed units. Timing is relative to the nominal MCU clock, not calibrated SI seconds.

The right-side target identifies itself as `NeuroMotion-4ch-v2-right-drdy` and adds optional `drdy_a="PB8"`, `drdy_b="PB2"`, `start_pin="PB4"`, `capture_map="TIM2_CH1_CH4_TTL_CH3"`. These fields describe wiring only; native record, time units, CRC and channel ordering are unchanged. Existing clients accept and retain extra metadata fields.

Optional startup diagnostics: `miso_probe="startup_idle_v1"`, `miso_idle_pullup`, `miso_idle_pulldown`, and `cs_idle` each contain three observations: after releasing both PWDN pins, after A initialization, and after B initialization. With both chip selects high, PA6 is briefly configured as an input with weak pull-up/down, then restored to SPI1 AF5 without pulls before any sampling. A pull-up reading of 1 and pull-down reading of 0 is consistent with an idle released line under those conditions; it does not prove release during another device's transfer or provide a voltage measurement. `cs_idle` bits 0/1 are the MCU-side A/B chip-select input readbacks. This diagnostic never drives MISO and does not run while sampling. Older clients may ignore these additional fields.

## Native record

SAMPLES contains one or more 28-byte records, normally 8 per frame. Full batches produce about 60000 bytes/s at 2000 native records/s (65.1% of theoretical UART capacity). Partial batches flush after at most 10 ms. Metadata/status/edges add overhead; actual sustained occupancy is verified separately.

| Offset | Type | Value |
| --- | --- | --- |
| 0 | uint8 | device in bits 0–1 (0=A, 1=B); flags in bits 2–7 |
| 1 | 3 bytes | ADC status uint24 |
| 4 | uint32 | device native conversion index, advanced at DRDY capture |
| 8 | uint64 | hardware-captured DRDY time, extended nominal microseconds |
| 16 | int32 | native channel 1 ADC code, sign-extended from 24 bits |
| 20 | int32 | native channel 2 ADC code |
| 24 | uint32 | device cumulative known dropped/invalid conversion count |

Flags: VALID=0x04, SPI_ERROR=0x08, STATUS_ERROR=0x10, READ_LATE=0x20, CAPTURE_ERROR=0x40; 0x80 is reserved. Valid data require VALID, no error flags, status prefix 0xC and signed-24-bit codes. Fields in an invalid record remain diagnostic and must not be interpreted as a valid zero measurement. Capture overrun cannot provide an exact count of unseen physical edges: it increments its own counter and resets that device's native index so the PC opens a new clock segment. Counters are not additive estimates of total lost samples.

Channel mapping: A local channels -> CH1/CH2; B -> CH3/CH4. Input microvolts = code × vref_uv / (2^23 × gain). Original codes are retained in native CSV; reconstructed grid values are explicitly labelled aligned, not native raw data.

SYNC_EDGE payload is `<uint32 edge_index, uint64 time_us, uint8 level, 3 reserved bytes>` (16 bytes). Edges are captured from the raw PA8 output looped to PB10, using the same TIM2 as both DRDY inputs. The first rising edge is the start-code anchor. Lost/invalid edge sequences invalidate TTL continuity; they are not replaced by fabricated periodic events.

STATUS reports per-device capture overrun, SPI errors, invalid ADC status, late reads and dropped counts, plus edge/record/transport losses and maximum read delay. TTL counters distinguish `edge_overcapture` (capture register overwritten) and `edge_queue_dropped` (software edge queue full); `edge_dropped` retains the aggregate for compatibility. These counters do not provide an exact count of unseen physical edges and must not be added to each other as a sample-loss estimate. Interleaved devices need not be globally timestamp ordered; each device must have validated monotonic native indices/times within a segment. PC arrival times never replace captured times.

## Hardware resources

SPI1 MISO PA6 and MOSI PA7 are shared, CS A=PB9, CS B=PB6. In the current `NeuroMotion-4ch-v2-split-sclk` firmware, SCLK A=PA5 and SCLK B=PB3. Before asserting CS, `route_sclk()` disables idle SPI and selects only one AF5 clock output; the other pin is held low as GPIO. The prior transfer is complete and both CS are high at this change. META contains `sclk_a`, `sclk_b`, and `sclk_routing="selected_only"`. PB3 is unavailable for SWO in this variant. GPIO AF5 mapping is documented in ST DS10693 Table 11: https://www.st.com/resource/en/datasheet/stm32f446re.pdf

This routing addresses observed whole-cycle DRDY misses in the old common-SCLK arrangement; TI documents that SCLK can raise DRDY even with CS high (section 8.5.1.6). The ADS sampling oscillators/CLKSEL are unchanged. START=PB8 is shared once at startup. PWDN A=PC8/B=PC9. DRDY A=PA0 TIM2_CH1, B=PA1 TIM2_CH2. TTL=PA8 TIM1_CH1, captured through PB10 TIM2_CH3. TIM2 runs at nominal 1 MHz with 64-bit wrap extension and short capture interrupts. USART2 TX uses DMA1 Stream6 Channel4; TIM2 uses capture interrupts, so it does not compete for that DMA stream. See ST RM0390 DMA request mapping, Table 28: https://www.st.com/resource/en/reference_manual/dm00135183.pdf

The separate `NeuroMotion_4CH_Right` target moves DRDY A to PB8 AF1 TIM2_CH1, DRDY B to PB2 AF1 TIM2_CH4, and shared START to PB4 GPIO. TTL remains PB10 TIM2_CH3 on the same 32-bit counter with unchanged overflow extension. PB4 cannot simultaneously serve JTAG NJTRST; SWD PA13/PA14 remains available. All other connections are unchanged. AF mapping: [ST DS10693 Table 11](https://www.st.com/resource/en/datasheet/stm32f446re.pdf). Header locations: [UM1724 Table 29](https://www.st.com/resource/en/user_manual/dm00105823.pdf).

Only module A enables its existing RLD configuration; B's RLD amplifier and sensing are disabled. Electrode/bias wiring must follow the separately reviewed hardware instructions. The v1 CubeMX file describes the legacy pinout; four-channel manual initialization is isolated in `dual_adc.c` and `nm_capture_map.h`, with its resource map recorded here. Regenerating legacy CubeMX output does not replace that initialization.

TTL capture alone uses IC3F=0xF: sampling at fDTS/32 with eight consecutive stable samples. With the current nominal 84 MHz timer input and CKD=0, this is approximately 3 microseconds of input filtering, not eight samples of the prescaled 1 MHz counter. It adds a small hardware capture delay; analog and video timing acceptance must account for the capture path. DRDY filters remain unchanged. META records `ttl_capture_filter="DTS_div32_N8"`; no software periodically fabricates missing edges.
