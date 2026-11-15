"""Create an isolated Keil diagnostic project from the recorded baseline."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2] / "NeuroMotion_MCU"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Refresh generated sources from the retained baseline")
    args = parser.parse_args()
    folder = ROOT / "mcu_diagnosis"
    active = json.loads((folder / "active_run.json").read_text(encoding="utf-8"))
    source, target = Path(active["baseline"]), folder / "project"
    if target.exists() and not args.refresh:
        raise RuntimeError("Isolated project already exists; retain it and use a new explicit project path")
    for name in ("Core", "Drivers"):
        if not (target / name).exists():
            shutil.copytree(source / name, target / name)
    if args.refresh:
        for name in ("main.c", "ads1292.c", "sample_stream.c", "stm32f4xx_it.c", "spi.c"):
            shutil.copy2(source / "Core/Src" / name, target / "Core/Src" / name)
    mdk = target / "MDK-ARM"
    mdk.mkdir(exist_ok=True)
    shutil.copy2(source / "MDK-ARM/startup_stm32f446xx.s", mdk)
    if not (mdk / "RTE").exists():
        shutil.copytree(source / "MDK-ARM/RTE", mdk / "RTE")
    shutil.copy2(source / "ads1292_f446re.ioc", target)
    for name in ("nm_diag.h", "nm_diag.c"):
        shutil.copy2(folder / "overrides" / name, target / "Core" / ("Inc" if name.endswith(".h") else "Src") / name)
    path = target / "Core/Src/main.c"
    text = path.read_text(encoding="utf-8")
    text = text.replace('#include "gpio.h"', '#include "gpio.h"\n#include "nm_diag.h"', 1)
    text = text.replace('  MX_SPI1_Init();', '#if !defined(MCU_DIAG_UART) && !defined(MCU_DIAG_GPIO)\n  MX_SPI1_Init();\n#endif', 1)
    text = text.replace('  MX_GPIO_Init();', '  MX_GPIO_Init();\n#ifdef MCU_DIAG_POLL\n  HAL_NVIC_DisableIRQ(EXTI9_5_IRQn);\n#endif', 1)
    text = text.replace('  MX_TIM1_Init();\n  MX_DAC_Init();', '#ifndef MCU_DIAG_UART\n  MX_TIM1_Init();\n  MX_DAC_Init();\n#endif', 1)
    text = text.replace('  SyncSignal_InitIdle();', '''#ifdef MCU_DIAG_UART
  NM_DiagUartRun();
#elif defined(MCU_DIAG_GPIO)
  NM_DiagGpioRun();
#elif defined(MCU_DIAG_ID_ONLY)
  ADS1292_DiagIdRun();
#elif defined(MCU_DIAG_SPI_LOOPBACK)
  NM_DiagSpiLoopbackRun();
#else
#if defined(MCU_DIAG_DRDY_ONLY)
  NM_DiagInit(6U);
#elif defined(MCU_DIAG_POLL)
  NM_DiagInit(5U);
#elif defined(MCU_DIAG_RAM_ONLY)
  NM_DiagInit(4U);
#else
  NM_DiagInit(2U);
#endif
  SyncSignal_InitIdle();''', 1)
    text = text.replace('  if (ADS1292_Init() != HAL_OK)\n  {\n    Error_Handler();\n  }', '''  HAL_StatusTypeDef ads_init = ADS1292_Init();
  NM_DiagHeartbeat();
  g_nm_diag.init_status = (uint32_t)ads_init;
  g_nm_diag.device_id = ADS1292_ReadDeviceId();
  g_nm_diag.ads_error = ADS1292_GetLastError();
  if (ads_init != HAL_OK) {
    g_nm_diag.fault_tick = HAL_GetTick();
#ifdef MCU_DIAG_REGCHECK
    ADS1292_DiagProbe(1U);
    for (;;) { NM_DiagHeartbeat(); }
#else
    g_nm_diag.stage = NM_STAGE_ERROR;
    Error_Handler();
#endif
  }
  g_nm_diag.stage = NM_STAGE_WAIT;''', 1)
    text = text.replace('  if (ADS1292_Start() != HAL_OK)', '''  g_nm_diag.start_ms = HAL_GetTick();
  g_nm_diag.stage = NM_STAGE_RUNNING;
  if (ADS1292_Start() != HAL_OK)''', 1)
    text = text.replace('  if (ADS1292_Start() != HAL_OK)\n  {\n    Error_Handler();\n  }', '''  if (ADS1292_Start() != HAL_OK)
  {
#ifdef MCU_DIAG_REGCHECK
    g_nm_diag.ads_error = ADS1292_GetLastError();
    g_nm_diag.fault_tick = HAL_GetTick();
    ADS1292_DiagProbe(4U);
    for (;;) { NM_DiagHeartbeat(); }
#else
    Error_Handler();
#endif
  }''', 1)
    text = text.replace('    SyncSignal_Process();', '''    NM_DiagHeartbeat();
#ifdef MCU_DIAG_REGCHECK
    static uint32_t last_count, changed_ms;
    uint32_t now = HAL_GetTick();
    if ((changed_ms == 0U) || (g_nm_diag.samples != last_count)) {
      last_count = g_nm_diag.samples;
      changed_ms = now;
    }
    if ((g_nm_diag.invalid_status != 0U) || ((now - changed_ms) >= 4000U)) {
      g_nm_diag.fault_tick = now;
      ADS1292_DiagProbe(g_nm_diag.invalid_status != 0U ? 3U : 2U);
      for (;;) { NM_DiagHeartbeat(); }
    }
#endif
    g_nm_diag.buffer_bytes = ADS1292_GetStreamByteCount();
    g_nm_diag.dropped = ADS1292_GetDroppedSamples();
    SyncSignal_Process();''', 1)
    text = text.replace('    SampleStream_Process();', '''#ifdef MCU_DIAG_POLL
    static uint8_t drdy_was_high = 1U;
    if (HAL_GPIO_ReadPin(ADS1292_DRDY_GPIO_Port, ADS1292_DRDY_Pin) == GPIO_PIN_SET) {
      drdy_was_high = 1U;
    } else if (drdy_was_high != 0U) {
      drdy_was_high = 0U;
      g_nm_diag.gpio_reserved0++;
      ADS1292_DRDY_Callback(ADS1292_DRDY_Pin);
    }
#endif
    SampleStream_Process();''', 1)
    text = text.replace('  /* USER CODE END 2 */', '#endif\n  /* USER CODE END 2 */', 1)
    path.write_text(text, encoding="utf-8")

    path = target / "Core/Src/ads1292.c"
    text = path.read_text(encoding="utf-8").replace('#include "spi.h"', '#include "spi.h"\n#include "nm_diag.h"', 1)
    text = text.replace('  return HAL_SPI_TransmitReceive(&hspi1, tx, rx, len, 10U);', '''#ifdef MCU_DIAG_SOFT_SPI
  return NM_DiagSoftSpi(tx, rx, len);
#else
  return HAL_SPI_TransmitReceive(&hspi1, tx, rx, len, 10U);
#endif''', 1)
    text = text.replace('  device_id = ads_regs[ADS1292_REG_ID];', '''  g_nm_diag.registers_0_3 = ((uint32_t)ads_regs[0] << 24) | ((uint32_t)ads_regs[1] << 16) | ((uint32_t)ads_regs[2] << 8) | ads_regs[3];
  g_nm_diag.registers_4_7 = ((uint32_t)ads_regs[4] << 24) | ((uint32_t)ads_regs[5] << 16) | ((uint32_t)ads_regs[6] << 8) | ads_regs[7];
  g_nm_diag.registers_8_11 = ((uint32_t)ads_regs[8] << 24) | ((uint32_t)ads_regs[9] << 16) | ((uint32_t)ads_regs[10] << 8) | ads_regs[11];
  device_id = ads_regs[ADS1292_REG_ID];''', 1)
    text = text.replace('  ADS1292_CS_HIGH();\n  ADS1292_DelayCommand();', '''#ifdef MCU_DIAG_CS_HOLD
  ADS1292_DelayCommand(); /* Conservative main-context CS hold for timing comparison. */
#endif
  ADS1292_CS_HIGH();
  ADS1292_DelayCommand();''')
    text = text.replace('  uint32_t current_index = sample_index++;', '  uint32_t current_index = sample_index++;\n  g_nm_diag.samples = sample_index;', 1)
    text = text.replace('    dropped_samples++;', '    dropped_samples++;\n    g_nm_diag.dropped = dropped_samples;', 1)
    text = text.replace('  ADS1292_PWDN_HIGH();\n  HAL_Delay(100U);', '''  ADS1292_PWDN_HIGH();
#ifdef MCU_DIAG_WAIT1S
  HAL_Delay(1000U);
#else
  HAL_Delay(100U);
#endif''', 1)
    old = '''  if (ADS1292_SPI_TransmitReceive(tx, rx, sizeof(rx)) != HAL_OK)
  {
    last_error = 10U;
    return;
  }'''
    new = '''  g_nm_diag.last_phase = 30U;
  HAL_StatusTypeDef result = ADS1292_SPI_TransmitReceive(tx, rx, sizeof(rx));
  g_nm_diag.last_hal_status = (uint32_t)result;
  g_nm_diag.last_spi_ms = HAL_GetTick();
  g_nm_diag.last_phase = 31U;
  if (result != HAL_OK) {
    g_nm_diag.spi_error++;
    last_error = 10U;
    g_nm_diag.ads_error = last_error;
    return;
  }
  g_nm_diag.spi_ok++;
  g_nm_diag.raw_status = ((uint32_t)rx[0] << 16) | ((uint32_t)rx[1] << 8) | rx[2];
  if (g_nm_diag.invalid_status == 0U) {
    /* Preserve the first malformed packet rather than a later valid packet. */
    g_nm_diag.response_0_3 = ((uint32_t)rx[0] << 24) | ((uint32_t)rx[1] << 16) | ((uint32_t)rx[2] << 8) | rx[3];
    g_nm_diag.response_4_7 = ((uint32_t)rx[4] << 24) | ((uint32_t)rx[5] << 16) | ((uint32_t)rx[6] << 8) | rx[7];
    g_nm_diag.response_8 = rx[8];
  }
  if ((g_nm_diag.raw_status & 0xF01FFFU) == 0xC00000U) {
    g_nm_diag.valid_status++;
  } else {
    g_nm_diag.invalid_status++;
  }'''
    assert old in text
    path.write_text(text.replace(old, new, 1) + (folder / "overrides/ads_fault_probe.inc").read_text(encoding="utf-8"), encoding="utf-8")

    path = target / "Core/Src/stm32f4xx_it.c"
    text = path.read_text(encoding="utf-8").replace('#include "ads1292.h"', '#include "ads1292.h"\n#include "nm_diag.h"', 1)
    text = text.replace('  HAL_IncTick();', '  HAL_IncTick();\n  g_nm_diag.tick_ms = HAL_GetTick();', 1)
    text = text.replace('  HAL_GPIO_EXTI_IRQHandler(ADS1292_DRDY_Pin);', '''  g_nm_diag.irq_enter++;
  g_nm_diag.last_irq_ms = HAL_GetTick();
  g_nm_diag.last_phase = 20U;
  HAL_GPIO_EXTI_IRQHandler(ADS1292_DRDY_Pin);''', 1)
    text = text.replace('  ADS1292_DRDY_Callback(ADS1292_DRDY_Pin);', '  if ((g_nm_diag.mode != 3U) && (g_nm_diag.mode != 6U)) { ADS1292_DRDY_Callback(ADS1292_DRDY_Pin); }\n  g_nm_diag.irq_exit++;\n  g_nm_diag.last_phase = 21U;', 1)
    path.write_text(text, encoding="utf-8")

    path = target / "Core/Src/sample_stream.c"
    text = path.read_text(encoding="utf-8").replace('#include "usart.h"', '#include "usart.h"\n#include "nm_diag.h"', 1)
    text = text.replace('  stream_frame[SAMPLE_STREAM_FRAME_SIZE - 1U] = SAMPLE_STREAM_FRAME_END;', '''#ifdef MCU_DIAG_RAM_ONLY
  return; /* Drain the same ring without UART in both paired experiments. */
#endif
  stream_frame[SAMPLE_STREAM_FRAME_SIZE - 1U] = SAMPLE_STREAM_FRAME_END;''', 1)
    text = text.replace('  (void)HAL_UART_Transmit(&huart2, stream_frame, SAMPLE_STREAM_FRAME_SIZE, 0xFFFFU);', '''  g_nm_diag.tx_attempt++;
  g_nm_diag.last_phase = 40U;
  HAL_StatusTypeDef result = HAL_UART_Transmit(&huart2, stream_frame, SAMPLE_STREAM_FRAME_SIZE, 0xFFFFU);
  g_nm_diag.last_hal_status = (uint32_t)result;
  if (result == HAL_OK) { g_nm_diag.tx_ok++; } else { g_nm_diag.tx_error++; }
  g_nm_diag.last_phase = 41U;''', 1)
    path.write_text(text, encoding="utf-8")
    path = target / "Core/Src/spi.c"
    text = path.read_text(encoding="utf-8")
    original = "  hspi1.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_64;"
    assert original in text
    text = text.replace(original, """#ifdef MCU_DIAG_SPI_DIV256
  hspi1.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_256;
#elif defined(MCU_DIAG_SLOW_SPI)
  hspi1.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_128;
#else
  hspi1.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_64;
#endif""", 1)
    original_speed = "    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_VERY_HIGH;"
    assert text.count(original_speed) == 1
    text = text.replace(original_speed, """#ifdef MCU_DIAG_SLEW_LOW
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
#else
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
#endif""", 1)
    path.write_text(text, encoding="utf-8")
    project = ET.parse(source / "MDK-ARM/ads1292_f446re.uvprojx")
    selected = project.find("./Targets/Target")
    selected.find("TargetName").text = "D_UART"
    common = selected.find("./TargetOption/TargetCommonOption")
    common.find("OutputDirectory").text = "build\\D_UART\\"
    common.find("OutputName").text = "D_UART"
    common.find("ListingPath").text = "build\\D_UART\\"
    for node in common.findall("./AfterMake/*"):
        if node.tag.startswith("RunUserProg"):
            node.text = "0"
    define = selected.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define")
    define.text += ",MCU_DIAG_UART"
    groups = selected.find("Groups")
    files = groups.find("./Group/Files")
    entry = ET.SubElement(files, "File")
    for name, value in (("FileName", "nm_diag.c"), ("FileType", "1"), ("FilePath", "..\\Core\\Src\\nm_diag.c")):
        ET.SubElement(entry, name).text = value
    adc = deepcopy(selected)
    adc.find("TargetName").text = "D_ADC"
    common_adc = adc.find("./TargetOption/TargetCommonOption")
    common_adc.find("OutputDirectory").text = "build\\D_ADC\\"
    common_adc.find("OutputName").text = "D_ADC"
    common_adc.find("ListingPath").text = "build\\D_ADC\\"
    adc.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text = "USE_HAL_DRIVER,STM32F446xx,MCU_DIAG_ADC"
    project.find("Targets").append(adc)
    wait = deepcopy(adc)
    wait.find("TargetName").text = "D_ADC_WAIT1S"
    common_wait = wait.find("./TargetOption/TargetCommonOption")
    common_wait.find("OutputDirectory").text = "build\\D_ADC_WAIT1S\\"
    common_wait.find("OutputName").text = "D_ADC_WAIT1S"
    common_wait.find("ListingPath").text = "build\\D_ADC_WAIT1S\\"
    wait.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_WAIT1S"
    project.find("Targets").append(wait)
    gpio = deepcopy(adc)
    gpio.find("TargetName").text = "D_GPIO_SELFTEST"
    common_gpio = gpio.find("./TargetOption/TargetCommonOption")
    common_gpio.find("OutputDirectory").text = "build\\D_GPIO_SELFTEST\\"
    common_gpio.find("OutputName").text = "D_GPIO_SELFTEST"
    common_gpio.find("ListingPath").text = "build\\D_GPIO_SELFTEST\\"
    gpio.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text = "USE_HAL_DRIVER,STM32F446xx,MCU_DIAG_GPIO"
    project.find("Targets").append(gpio)
    for label, extra in (("D_ADC_RAM", "MCU_DIAG_RAM_ONLY"), ("D_POLL_RAM", "MCU_DIAG_RAM_ONLY,MCU_DIAG_POLL")):
        ram = deepcopy(wait)
        ram.find("TargetName").text = label
        common_ram = ram.find("./TargetOption/TargetCommonOption")
        for name in ("OutputDirectory", "ListingPath"):
            common_ram.find(name).text = f"build\\{label}\\"
        common_ram.find("OutputName").text = label
        ram.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += "," + extra
        project.find("Targets").append(ram)
        slow = deepcopy(ram)
        slow_label = label + "_SLOW"
        slow.find("TargetName").text = slow_label
        slow_common = slow.find("./TargetOption/TargetCommonOption")
        for name in ("OutputDirectory", "ListingPath"):
            slow_common.find(name).text = f"build\\{slow_label}\\"
        slow_common.find("OutputName").text = slow_label
        slow.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_SLOW_SPI"
        project.find("Targets").append(slow)
        if label == "D_ADC_RAM":
            hold = deepcopy(ram)
            hold.find("TargetName").text = "D_ADC_RAM_CS_HOLD"
            common_hold = hold.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                common_hold.find(name).text = "build\\D_ADC_RAM_CS_HOLD\\"
            common_hold.find("OutputName").text = "D_ADC_RAM_CS_HOLD"
            hold.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_CS_HOLD"
            project.find("Targets").append(hold)
            slow_hold = deepcopy(hold)
            label_hold = "D_ADC_RAM_CS_HOLD_DIV256"
            slow_hold.find("TargetName").text = label_hold
            options = slow_hold.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                options.find(name).text = f"build\\{label_hold}\\"
            options.find("OutputName").text = label_hold
            slow_hold.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_SPI_DIV256"
            project.find("Targets").append(slow_hold)
            drdy = deepcopy(slow_hold)
            drdy.find("TargetName").text = "D_DRDY_ONLY"
            options = drdy.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                options.find(name).text = "build\\D_DRDY_ONLY\\"
            options.find("OutputName").text = "D_DRDY_ONLY"
            drdy.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_DRDY_ONLY"
            project.find("Targets").append(drdy)
            slew = deepcopy(slow_hold)
            label_slew = "D_ADC_RAM_GPIO_LOW"
            slew.find("TargetName").text = label_slew
            options = slew.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                options.find(name).text = f"build\\{label_slew}\\"
            options.find("OutputName").text = label_slew
            slew.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_SLEW_LOW"
            project.find("Targets").append(slew)
            probe = deepcopy(slew)
            label_probe = "D_ADC_RAM_PROBE"
            probe.find("TargetName").text = label_probe
            options = probe.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                options.find(name).text = f"build\\{label_probe}\\"
            options.find("OutputName").text = label_probe
            probe.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_REGCHECK"
            project.find("Targets").append(probe)
            id_only = deepcopy(probe)
            label_id = "D_ID_ONLY"
            id_only.find("TargetName").text = label_id
            options = id_only.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                options.find(name).text = f"build\\{label_id}\\"
            options.find("OutputName").text = label_id
            id_only.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_ID_ONLY"
            project.find("Targets").append(id_only)
            loopback = deepcopy(slew)
            label_loopback = "D_SPI_LOOPBACK"
            loopback.find("TargetName").text = label_loopback
            options = loopback.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                options.find(name).text = f"build\\{label_loopback}\\"
            options.find("OutputName").text = label_loopback
            loopback.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_SPI_LOOPBACK"
            project.find("Targets").append(loopback)
            soft = deepcopy(hold)
            label_soft = "D_ADC_RAM_SOFTSPI"
            soft.find("TargetName").text = label_soft
            options = soft.find("./TargetOption/TargetCommonOption")
            for name in ("OutputDirectory", "ListingPath"):
                options.find(name).text = f"build\\{label_soft}\\"
            options.find("OutputName").text = label_soft
            soft.find("./TargetOption/TargetArmAds/Cads/VariousControls/Define").text += ",MCU_DIAG_SOFT_SPI"
            project.find("Targets").append(soft)
    project.write(mdk / "mcu_diagnosis.uvprojx", encoding="utf-8", xml_declaration=True)
    print(mdk / "mcu_diagnosis.uvprojx")


if __name__ == "__main__":
    main()
