#ifndef NM_CAPTURE_MAP_H
#define NM_CAPTURE_MAP_H

/* Independent Keil targets select a complete pin/capture map. Both profiles
 * use the same 32-bit TIM2 counter, microsecond prescaler and TTL CH3. */
#ifdef NEUROMOTION_RIGHT_DRDY
#define NM_DRDY_PORT_B          1U
#define NM_DRDY_A_PIN_NUMBER    8U
#define NM_DRDY_B_PIN_NUMBER    2U
#define NM_DRDY_B_CHANNEL       4U
#define NM_START_PIN_NUMBER     4U
#define NM_FIRMWARE_NAME        "NeuroMotion-4ch-v2-right-drdy"
#define NM_DRDY_METADATA        "\"drdy_a\":\"PB8\",\"drdy_b\":\"PB2\",\"start_pin\":\"PB4\",\"capture_map\":\"TIM2_CH1_CH4_TTL_CH3\","
#else
#define NM_DRDY_PORT_B          0U
#define NM_DRDY_A_PIN_NUMBER    0U
#define NM_DRDY_B_PIN_NUMBER    1U
#define NM_DRDY_B_CHANNEL       2U
#define NM_START_PIN_NUMBER     8U
#define NM_FIRMWARE_NAME        "NeuroMotion-4ch-v2-split-sclk"
#define NM_DRDY_METADATA        ""
#endif

#define NM_DRDY_PIN_MASK ((1U << NM_DRDY_A_PIN_NUMBER) | (1U << NM_DRDY_B_PIN_NUMBER))
#define NM_DRDY_A_IF (1U << 1U)
#define NM_DRDY_B_IF (1U << NM_DRDY_B_CHANNEL)
#define NM_DRDY_A_OF (1U << 9U)
#define NM_DRDY_B_OF (1U << (NM_DRDY_B_CHANNEL + 8U))
#define NM_DRDY_SR_MASK (NM_DRDY_A_IF | NM_DRDY_B_IF | NM_DRDY_A_OF | NM_DRDY_B_OF)

#endif
