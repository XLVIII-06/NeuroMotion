#ifndef __SYNC_SIGNAL_H__
#define __SYNC_SIGNAL_H__

#ifdef __cplusplus
extern "C" {
#endif

#include "main.h"
#include <stdint.h>

#define SYNC_SIGNAL_BACKEND_DAC   1U
#define SYNC_SIGNAL_BACKEND_TIM1  2U

#ifndef SYNC_SIGNAL_BACKEND
//#define SYNC_SIGNAL_BACKEND SYNC_SIGNAL_BACKEND_DAC  //Use DAC produce TTL
#define SYNC_SIGNAL_BACKEND SYNC_SIGNAL_BACKEND_TIM1  //Use Timer1_CH1 produce TTL. ATTENTION: PA_8 output 3.3V, if input to BL-420N, resistor voltage division is required.
#endif

void SyncSignal_InitIdle(void);
void SyncSignal_Start(void);
void SyncSignal_RequestStop(void);
void SyncSignal_Process(void);
uint8_t SyncSignal_GetLevel(void);

#ifdef __cplusplus
}
#endif

#endif /* __SYNC_SIGNAL_H__ */
