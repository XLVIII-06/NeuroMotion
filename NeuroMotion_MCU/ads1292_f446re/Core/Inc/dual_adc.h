#ifndef NM_DUAL_ADC_H
#define NM_DUAL_ADC_H
#include <stdint.h>

/* Four-channel target only; legacy target retains its original pinout/protocol. */
uint8_t DualADC_Init(void);
void DualADC_Start(void);
void DualADC_Process(void);
void DualADC_TimerIRQ(void);
void DualADC_DMAIRQ(void);
void DualADC_UARTIRQ(void);
uint64_t DualADC_TimeUs(void);
#endif
