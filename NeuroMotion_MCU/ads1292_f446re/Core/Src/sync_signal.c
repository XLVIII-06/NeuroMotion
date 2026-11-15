#include "sync_signal.h"

#if (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_DAC)
#include "dac.h"
#elif (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_TIM1)
#include "tim.h"
#else
#error "Unsupported SYNC_SIGNAL_BACKEND"
#endif

#define SYNC_DAC_LOW_CODE      0U
#define SYNC_DAC_HIGH_CODE     620U
#define SYNC_STABLE_HALF_MS    500U
#define SYNC_BUTTON_DEBOUNCE_MS 50U
#define SYNC_STOP_CYCLE_COUNT  5U

typedef enum
{
  SYNC_STATE_IDLE = 0,
  SYNC_STATE_START_PATTERN,
  SYNC_STATE_STABLE,
  SYNC_STATE_WAIT_STOP_CYCLES,
  SYNC_STATE_STOP_CYCLES,
  SYNC_STATE_END_PATTERN,
  SYNC_STATE_STOPPED
} SyncState_t;

typedef struct
{
  GPIO_PinState level;
  uint32_t duration_ms;
} SyncPatternStep_t;

static const SyncPatternStep_t sync_start_pattern[] =
{
  /* Seven-bit Barker code 1110010, 500 ms per symbol. */
  {GPIO_PIN_SET,   500U},
  {GPIO_PIN_SET,   500U},
  {GPIO_PIN_SET,   500U},
  {GPIO_PIN_RESET, 500U},
  {GPIO_PIN_RESET, 500U},
  {GPIO_PIN_SET,   500U},
  {GPIO_PIN_RESET, 500U}
};

static const SyncPatternStep_t sync_end_pattern[] =
{
  /* Seven-bit inverted Barker code 0001101, 500 ms per symbol. */
  {GPIO_PIN_RESET, 500U},
  {GPIO_PIN_RESET, 500U},
  {GPIO_PIN_RESET, 500U},
  {GPIO_PIN_SET,   500U},
  {GPIO_PIN_SET,   500U},
  {GPIO_PIN_RESET, 500U},
  {GPIO_PIN_SET,   500U}
};

static SyncState_t sync_state = SYNC_STATE_IDLE;
static const SyncPatternStep_t *active_pattern;
static uint8_t active_pattern_length;
static uint8_t pattern_index;
static uint32_t step_started_ms;
static uint32_t stable_toggled_ms;
static uint8_t current_level;
static volatile uint8_t stop_requested;
static volatile uint8_t button_event_seen;
static volatile uint32_t last_button_event_ms;
static uint8_t stable_cycle_count;
#if (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_TIM1)
static uint16_t stable_previous_counter;
#endif

static void SyncSignal_WriteLevel(uint8_t level);
static void SyncSignal_StartPattern(const SyncPatternStep_t *pattern, uint8_t length, SyncState_t state);
static void SyncSignal_StartEndPattern(void);
static uint8_t SyncSignal_StableCycleBoundary(void);

#if (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_DAC)
static void SyncSignal_StartBackendIdle(void)
{
  HAL_DAC_Start(&hdac, DAC_CHANNEL_1);
  SyncSignal_WriteLevel(0U);
}

static void SyncSignal_StartBackendStable(void)
{
  stable_toggled_ms = HAL_GetTick();
  SyncSignal_WriteLevel(0U);
}

static void SyncSignal_WriteLevel(uint8_t level)
{
  uint32_t dac_code = (level != 0U) ? SYNC_DAC_HIGH_CODE : SYNC_DAC_LOW_CODE;

  HAL_DAC_SetValue(&hdac, DAC_CHANNEL_1, DAC_ALIGN_12B_R, dac_code);
  current_level = (level != 0U) ? 1U : 0U;
}

static void SyncSignal_ProcessStable(void)
{
  uint32_t now = HAL_GetTick();

  if ((now - stable_toggled_ms) >= SYNC_STABLE_HALF_MS)
  {
    stable_toggled_ms = now;
    SyncSignal_WriteLevel((current_level == 0U) ? 1U : 0U);
  }
}

static uint8_t SyncSignal_StableCycleBoundary(void)
{
  return 0U;
}
#endif

#if (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_TIM1)
static void SyncSignal_ConfigTim1GPIO(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};

  HAL_TIM_PWM_Stop(&htim1, TIM_CHANNEL_1);

  __HAL_RCC_GPIOA_CLK_ENABLE();
  GPIO_InitStruct.Pin = GPIO_PIN_8;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);
}

static void SyncSignal_ConfigTim1PWM(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};

  __HAL_RCC_GPIOA_CLK_ENABLE();
  GPIO_InitStruct.Pin = GPIO_PIN_8;
  GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  GPIO_InitStruct.Alternate = GPIO_AF1_TIM1;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  __HAL_TIM_SET_COUNTER(&htim1, 0U);
  __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, 5000U);
  HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_1);
}

static void SyncSignal_StartBackendIdle(void)
{
  SyncSignal_ConfigTim1GPIO();
  SyncSignal_WriteLevel(0U);
}

static void SyncSignal_StartBackendStable(void)
{
  current_level = 0U;
  SyncSignal_ConfigTim1PWM();
}

static uint8_t SyncSignal_StableCycleBoundary(void)
{
  uint16_t counter = (uint16_t)__HAL_TIM_GET_COUNTER(&htim1);
  uint8_t wrapped = (counter < stable_previous_counter) ? 1U : 0U;

  stable_previous_counter = counter;
  return wrapped;
}

static void SyncSignal_WriteLevel(uint8_t level)
{
  HAL_GPIO_WritePin(GPIOA, GPIO_PIN_8, (level != 0U) ? GPIO_PIN_SET : GPIO_PIN_RESET);
  current_level = (level != 0U) ? 1U : 0U;
}

static void SyncSignal_ProcessStable(void)
{
}
#endif

void SyncSignal_InitIdle(void)
{
  active_pattern = NULL;
  active_pattern_length = 0U;
  pattern_index = 0U;
  stable_cycle_count = 0U;
  stop_requested = 0U;
  button_event_seen = 0U;
  last_button_event_ms = 0U;
  sync_state = SYNC_STATE_IDLE;
  SyncSignal_StartBackendIdle();
}

void SyncSignal_Start(void)
{
  SyncSignal_StartBackendIdle();
  SyncSignal_StartPattern(sync_start_pattern,
                          (uint8_t)(sizeof(sync_start_pattern) / sizeof(sync_start_pattern[0])),
                          SYNC_STATE_START_PATTERN);
}

void SyncSignal_RequestStop(void)
{
  uint32_t now = HAL_GetTick();

  if ((stop_requested != 0U) || (sync_state == SYNC_STATE_STOPPED))
  {
    return;
  }

  if ((button_event_seen != 0U) && ((now - last_button_event_ms) < SYNC_BUTTON_DEBOUNCE_MS))
  {
    return;
  }

  last_button_event_ms = now;
  button_event_seen = 1U;
  stop_requested = 1U;
}

void SyncSignal_Process(void)
{
  uint32_t now;

  if (sync_state == SYNC_STATE_STABLE)
  {
    SyncSignal_ProcessStable();

    if (stop_requested != 0U)
    {
#if (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_TIM1)
      stable_previous_counter = (uint16_t)__HAL_TIM_GET_COUNTER(&htim1);
#endif
      sync_state = SYNC_STATE_WAIT_STOP_CYCLES;
    }

    return;
  }

  if (sync_state == SYNC_STATE_WAIT_STOP_CYCLES)
  {
    SyncSignal_ProcessStable();
    if (SyncSignal_StableCycleBoundary() != 0U)
    {
      stable_cycle_count = 0U;
      sync_state = SYNC_STATE_STOP_CYCLES;
    }

    return;
  }

  if (sync_state == SYNC_STATE_STOP_CYCLES)
  {
    SyncSignal_ProcessStable();
    if (SyncSignal_StableCycleBoundary() != 0U)
    {
      stable_cycle_count++;
      if (stable_cycle_count >= SYNC_STOP_CYCLE_COUNT)
      {
        SyncSignal_StartEndPattern();
      }
    }

    return;
  }

  if ((sync_state != SYNC_STATE_START_PATTERN) && (sync_state != SYNC_STATE_END_PATTERN))
  {
    return;
  }

  now = HAL_GetTick();
  if ((now - step_started_ms) < active_pattern[pattern_index].duration_ms)
  {
    return;
  }

  pattern_index++;
  if (pattern_index >= active_pattern_length)
  {
    if (sync_state == SYNC_STATE_START_PATTERN)
    {
      __disable_irq();
      SyncSignal_StartBackendStable();
#if (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_TIM1)
      stable_previous_counter = (uint16_t)__HAL_TIM_GET_COUNTER(&htim1);
#endif
      stable_cycle_count = 0U;
      sync_state = (stop_requested != 0U) ? SYNC_STATE_STOP_CYCLES : SYNC_STATE_STABLE;
      __enable_irq();
    }
    else
    {
      SyncSignal_StartBackendIdle();
      sync_state = SYNC_STATE_STOPPED;
    }
    return;
  }

  step_started_ms = now;
  SyncSignal_WriteLevel((active_pattern[pattern_index].level == GPIO_PIN_SET) ? 1U : 0U);
}

uint8_t SyncSignal_GetLevel(void)
{
#if (SYNC_SIGNAL_BACKEND == SYNC_SIGNAL_BACKEND_TIM1)
  if ((sync_state == SYNC_STATE_STABLE) ||
      (sync_state == SYNC_STATE_WAIT_STOP_CYCLES) ||
      (sync_state == SYNC_STATE_STOP_CYCLES))
  {
    return (__HAL_TIM_GET_COUNTER(&htim1) < __HAL_TIM_GET_COMPARE(&htim1, TIM_CHANNEL_1)) ? 1U : 0U;
  }
#endif

  return current_level;
}

static void SyncSignal_StartPattern(const SyncPatternStep_t *pattern, uint8_t length, SyncState_t state)
{
  active_pattern = pattern;
  active_pattern_length = length;
  pattern_index = 0U;
  step_started_ms = HAL_GetTick();
  SyncSignal_WriteLevel((active_pattern[0].level == GPIO_PIN_SET) ? 1U : 0U);
  sync_state = state;
}

static void SyncSignal_StartEndPattern(void)
{
  SyncSignal_StartBackendIdle();
  SyncSignal_StartPattern(sync_end_pattern,
                          (uint8_t)(sizeof(sync_end_pattern) / sizeof(sync_end_pattern[0])),
                          SYNC_STATE_END_PATTERN);
}
