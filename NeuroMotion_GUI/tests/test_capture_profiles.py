"""Check actual firmware headers against CMSIS and the delivered wiring profiles."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from neuromotion.protocol_v2 import BOOT, V2Decoder, encode_packet
from neuromotion.simulated_v2 import simulation_metadata
from scripts.build_system_breadboard import make_layout


MCU = Path(__file__).resolve().parents[2] / 'NeuroMotion_MCU/ads1292_f446re'


class CaptureProfileTests(unittest.TestCase):
    def test_project_keeps_all_three_independent_targets(self):
        targets = {t.findtext('TargetName'): t for t in
                   ET.parse(MCU / 'MDK-ARM/ads1292_f446re.uvprojx').findall('./Targets/Target')}
        for name, flags in [('ads1292_f446re', set()),
                            ('NeuroMotion_4CH', {'NEUROMOTION_DUAL_ADC'}),
                            ('NeuroMotion_4CH_Right', {'NEUROMOTION_DUAL_ADC', 'NEUROMOTION_RIGHT_DRDY'})]:
            target = targets[name]
            defines = set(target.findtext('.//Cads/VariousControls/Define').split(','))
            self.assertEqual(defines & {'NEUROMOTION_DUAL_ADC', 'NEUROMOTION_RIGHT_DRDY'}, flags)
        self.assertNotEqual(targets['NeuroMotion_4CH'].findtext('.//OutputDirectory'),
                            targets['NeuroMotion_4CH_Right'].findtext('.//OutputDirectory'))

    def test_real_headers_match_cmsis_capture_flags_and_wiring(self):
        compiler = os.environ.get('NEUROMOTION_ARMCLANG') or shutil.which('armclang')
        if not compiler:
            self.skipTest('ARMCLANG required; set NEUROMOTION_ARMCLANG to validate firmware headers')
        includes = ['Core/Inc', 'Drivers/CMSIS/Include',
                    'Drivers/CMSIS/Device/ST/STM32F4xx/Include', 'Drivers/STM32F4xx_HAL_Driver/Inc']
        with tempfile.TemporaryDirectory() as folder:
            for profile, channel in [('original', 2), ('right', 4)]:
                with self.subTest(profile=profile):
                    pins = make_layout(profile)['mcu']
                    source = Path(folder) / (profile + '.c')
                    source.write_text(f'''#include "main.h"
#include "nm_capture_map.h"
_Static_assert(NM_DRDY_PORT_B == {int(profile == 'right')}, "DRDY port");
_Static_assert(NM_DRDY_A_PIN_NUMBER == {pins['drdy_a'][2:]}, "A pin");
_Static_assert(NM_DRDY_B_PIN_NUMBER == {pins['drdy_b'][2:]}, "B pin");
_Static_assert(ADS1292_START_Pin == GPIO_PIN_{pins['start'][2:]}, "START GPIO");
_Static_assert(NM_START_PIN_NUMBER == {pins['start'][2:]}, "START metadata");
_Static_assert(NM_DRDY_A_IF == TIM_SR_CC1IF, "A IRQ flag");
_Static_assert(NM_DRDY_B_IF == TIM_SR_CC{channel}IF, "B IRQ flag");
_Static_assert(NM_DRDY_A_IF == TIM_DIER_CC1IE, "A IRQ enable");
_Static_assert(NM_DRDY_B_IF == TIM_DIER_CC{channel}IE, "B IRQ enable");
_Static_assert(NM_DRDY_A_OF == TIM_SR_CC1OF, "A overcapture");
_Static_assert(NM_DRDY_B_OF == TIM_SR_CC{channel}OF, "B overcapture");
_Static_assert(!(NM_DRDY_SR_MASK & (TIM_SR_UIF | TIM_SR_CC3IF | TIM_SR_CC3OF)), "TTL/overflow independent");
''', encoding='utf-8')
                    flags = ['-DNEUROMOTION_DUAL_ADC']
                    if profile == 'right':
                        flags.append('-DNEUROMOTION_RIGHT_DRDY')
                    result = subprocess.run([compiler, '--target=arm-arm-none-eabi', '-mcpu=cortex-m4',
                                             '-mthumb', '-std=c11', '-Werror', '-fsyntax-only',
                                             '-DSTM32F446xx', '-DUSE_HAL_DRIVER', *flags,
                                             *['-I' + str(MCU / p) for p in includes], str(source)],
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_right_pin_metadata_remains_compatible_with_gui_decoder(self):
        meta = {**simulation_metadata(), 'firmware': 'NeuroMotion-4ch-v2-right-drdy',
                'drdy_a': 'PB8', 'drdy_b': 'PB2', 'start_pin': 'PB4',
                'capture_map': 'TIM2_CH1_CH4_TTL_CH3'}
        decoder = V2Decoder()
        packets = decoder.feed(encode_packet(BOOT, meta))
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].value, meta)
        self.assertEqual(decoder.invalid_records, 0)


if __name__ == '__main__':
    unittest.main()
