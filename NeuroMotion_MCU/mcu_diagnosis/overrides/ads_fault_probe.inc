
#ifdef MCU_DIAG_REGCHECK
/* Included into the isolated ADS driver so the existing static SPI helpers
 * are used. Only the main loop calls this, after preserving the first fault. */
static uint32_t diag_pack_registers(const uint8_t *bytes)
{
  return ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
         ((uint32_t)bytes[2] << 8) | bytes[3];
}

void ADS1292_DiagProbe(uint32_t reason)
{
  uint8_t registers[12] = {0};
  HAL_NVIC_DisableIRQ(EXTI9_5_IRQn);
  sampling_enabled = 0U;
  g_nm_diag.probe_reason = reason; /* 1 init, 2 stall, 3 bad status, 4 start. */
  g_nm_diag.probe_tick_ms = HAL_GetTick();
  g_nm_diag.probe_levels_before =
    ((uint32_t)HAL_GPIO_ReadPin(ADS1292_DRDY_GPIO_Port, ADS1292_DRDY_Pin)) |
    ((uint32_t)HAL_GPIO_ReadPin(ADS1292_START_GPIO_Port, ADS1292_START_Pin) << 1) |
    ((uint32_t)HAL_GPIO_ReadPin(ADS1292_PWDN_GPIO_Port, ADS1292_PWDN_Pin) << 2);
  g_nm_diag.buffer_bytes = ADS1292_GetStreamByteCount();
  g_nm_diag.dropped = ADS1292_GetDroppedSamples();
  g_nm_diag.stage = NM_STAGE_PROBING;
  g_nm_diag.probe_stage = 1U;
  ADS1292_START_LOW();
  ADS1292_CS_HIGH();
  ADS1292_DelayCommand();
  g_nm_diag.probe_stop_status = (uint32_t)ADS1292_SendCommand(ADS1292_CMD_STOP);
  g_nm_diag.probe_sdatac_status = (uint32_t)ADS1292_SendCommand(ADS1292_CMD_SDATAC);
  g_nm_diag.probe_stage = 2U;
  for (uint32_t i = 0U; i < 4U; i++) {
    uint8_t id = 0U;
    HAL_StatusTypeDef status = ADS1292_ReadRegisters(ADS1292_REG_ID, &id, 1U);
    g_nm_diag.post_ids_0_3 = (g_nm_diag.post_ids_0_3 << 8) | id;
    g_nm_diag.probe_id_status_bits |= ((uint32_t)status & 3U) << (2U * i);
  }
  g_nm_diag.probe_read_status = (uint32_t)ADS1292_ReadRegisters(ADS1292_REG_ID, registers, sizeof(registers));
  g_nm_diag.post_registers_0_3 = diag_pack_registers(&registers[0]);
  g_nm_diag.post_registers_4_7 = diag_pack_registers(&registers[4]);
  g_nm_diag.post_registers_8_11 = diag_pack_registers(&registers[8]);
  g_nm_diag.probe_stage = 3U;
  HAL_Delay(5U);
  memset(registers, 0, sizeof(registers));
  g_nm_diag.probe_repeat_read_status = (uint32_t)ADS1292_ReadRegisters(ADS1292_REG_ID, registers, sizeof(registers));
  g_nm_diag.post_repeat_0_3 = diag_pack_registers(&registers[0]);
  g_nm_diag.post_repeat_4_7 = diag_pack_registers(&registers[4]);
  g_nm_diag.post_repeat_8_11 = diag_pack_registers(&registers[8]);
  g_nm_diag.probe_stage = 4U;
  g_nm_diag.stage = NM_STAGE_PROBED;
}
#endif

#ifdef MCU_DIAG_ID_ONLY
/* Minimal communication experiment: no configuration writes or conversions. */
void ADS1292_DiagIdRun(void)
{
  uint8_t bytes[12] = {0};
  NM_DiagInit(8U);
  HAL_NVIC_DisableIRQ(EXTI9_5_IRQn);
  SyncSignal_InitIdle();
  ADS1292_START_LOW();
  ADS1292_CS_HIGH();
  ADS1292_PWDN_LOW();
  HAL_Delay(50U);
  ADS1292_PWDN_HIGH();
  HAL_Delay(1000U);
  g_nm_diag.probe_sdatac_status = (uint32_t)ADS1292_SendCommand(ADS1292_CMD_SDATAC);
  g_nm_diag.probe_stop_status = (uint32_t)ADS1292_SendCommand(ADS1292_CMD_STOP);
  g_nm_diag.last_hal_status = (uint32_t)ADS1292_SendCommand(ADS1292_CMD_RESET);
  HAL_Delay(10U);
  (void)ADS1292_SendCommand(ADS1292_CMD_SDATAC);
  for (uint32_t i = 0U; i < 4U; i++) {
    uint8_t id = 0U;
    HAL_StatusTypeDef status = ADS1292_ReadRegisters(ADS1292_REG_ID, &id, 1U);
    g_nm_diag.post_ids_0_3 = (g_nm_diag.post_ids_0_3 << 8) | id;
    g_nm_diag.probe_id_status_bits |= ((uint32_t)status & 3U) << (2U * i);
  }
  g_nm_diag.device_id = g_nm_diag.post_ids_0_3 >> 24;
  g_nm_diag.probe_read_status = (uint32_t)ADS1292_ReadRegisters(ADS1292_REG_ID, bytes, sizeof(bytes));
  g_nm_diag.registers_0_3 = diag_pack_registers(&bytes[0]);
  g_nm_diag.registers_4_7 = diag_pack_registers(&bytes[4]);
  g_nm_diag.registers_8_11 = diag_pack_registers(&bytes[8]);
  HAL_Delay(5U);
  memset(bytes, 0, sizeof(bytes));
  g_nm_diag.probe_repeat_read_status = (uint32_t)ADS1292_ReadRegisters(ADS1292_REG_ID, bytes, sizeof(bytes));
  g_nm_diag.post_repeat_0_3 = diag_pack_registers(&bytes[0]);
  g_nm_diag.post_repeat_4_7 = diag_pack_registers(&bytes[4]);
  g_nm_diag.post_repeat_8_11 = diag_pack_registers(&bytes[8]);
  g_nm_diag.init_status = (g_nm_diag.post_ids_0_3 == 0x73737373U &&
                          g_nm_diag.probe_id_status_bits == 0U &&
                          g_nm_diag.probe_read_status == 0U &&
                          g_nm_diag.probe_repeat_read_status == 0U) ? 0U : 1U;
  g_nm_diag.stage = g_nm_diag.init_status == 0U ? NM_STAGE_COMPLETE : NM_STAGE_ERROR;
  g_nm_diag.finish_ms = HAL_GetTick();
  for (;;) { NM_DiagHeartbeat(); }
}
#endif
