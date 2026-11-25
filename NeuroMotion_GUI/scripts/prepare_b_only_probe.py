"""Build an isolated B-only bench image; never change production sources or flash."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


def replace_once(path, before, after):
    text = path.read_text(encoding='utf-8-sig')
    if text.count(before) != 1:
        raise ValueError(f'Unexpected source layout: {path.name}: {before[:70]}')
    path.write_text(text.replace(before, after), encoding='utf-8')


def main():
    src = ROOT / 'NeuroMotion_MCU/ads1292_f446re'
    output = ROOT / 'NeuroMotion_GUI/output/b_only_probe'
    project = output / 'project'
    if project.exists():
        raise FileExistsError('Keep the existing probe archive; do not overwrite it')
    output.mkdir(parents=True, exist_ok=True)
    baseline = {str(p.relative_to(src)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [src/'Core/Src/main.c', src/'Core/Src/dual_adc.c', src/'Core/Inc/nm_capture_map.h',
                          src/'MDK-ARM/ads1292_f446re.uvprojx']}
    for folder in ['Core', 'Drivers']:
        shutil.copytree(src / folder, project / folder)
    mdk = project / 'MDK-ARM'
    mdk.mkdir()
    shutil.copy2(src / 'MDK-ARM/startup_stm32f446xx_4ch.s', mdk)
    tree = ET.parse(src / 'MDK-ARM/ads1292_f446re.uvprojx')
    targets = tree.find('Targets')
    for target in list(targets):
        if target.findtext('TargetName') != 'NeuroMotion_4CH_Right':
            targets.remove(target)
    target = targets.find('Target')
    target.find('TargetName').text = 'NeuroMotion_B_Only_Probe'
    target.find('.//OutputName').text = 'NeuroMotion_B_Only_Probe'
    target.find('.//OutputDirectory').text = 'build\\b_only\\'
    tree.write(mdk / 'ads1292_f446re.uvprojx', encoding='utf-8', xml_declaration=True)
    replace_once(project/'Core/Src/main.c',
                 'if (ready == 3U) { SyncSignal_Start(); DualADC_Start(); }',
                 'if (ready == 2U) { DualADC_Start(); } /* Isolated B-only diagnostic, no TTL. */')
    source = project/'Core/Src/dual_adc.c'
    replace_once(source, 'TIM2->DIER = TIM_DIER_UIE | NM_DRDY_A_IF | NM_DRDY_B_IF | TIM_DIER_CC3IE;',
                 'TIM2->CCER &= ~(TIM_CCER_CC1E | TIM_CCER_CC3E); /* No A or TTL input. */\n'
                 '  TIM2->DIER = TIM_DIER_UIE | NM_DRDY_B_IF;')
    replace_once(source, 'uint32_t sr = TIM2->SR;\n  for (uint8_t d = 0U; d < 2U; ++d)',
                 'uint32_t sr = TIM2->SR;\n  for (uint8_t d = 1U; d < 2U; ++d)')
    replace_once(source, 'for (uint8_t d = 0U; d < 2U; ++d)\n  {\n    dev[d].error = init_device(d);',
                 'dev[0].error = 0xFFU; /* A intentionally absent. */\n'
                 '  for (uint8_t d = 1U; d < 2U; ++d)\n  {\n    dev[d].error = init_device(d);')
    replace_once(source, 'if (ready_mask != 3U) return;\n  if (!command(0U, 0x10U) || !command(1U, 0x10U))',
                 'if (ready_mask != 2U) return;\n  if (!command(1U, 0x10U))')
    replace_once(source, '      NM_DRDY_METADATA\n',
                 '      NM_DRDY_METADATA\n'
                 '      "\\\"diagnostic_mode\\\":\\\"B_only\\\",\\\"ttl_capture_enabled\\\":false,"\n')
    replace_once(project/'Core/Inc/nm_capture_map.h',
                 '"NeuroMotion-4ch-v2-right-drdy"', '"NeuroMotion-B-only-probe"')
    sys.path.insert(0, str(ROOT / 'NeuroMotion_MCU'))
    from build_firmware import locate_uv4
    cli = locate_uv4()
    if cli is None:
        raise RuntimeError('Keil UV4 not found')
    log = output / 'build.log'
    result = subprocess.run([str(cli), '-b', str(mdk/'ads1292_f446re.uvprojx'),
                             '-t', 'NeuroMotion_B_Only_Probe', '-j0', '-o', str(log)],
                            timeout=150, creationflags=subprocess.CREATE_NO_WINDOW)
    image = mdk / 'build/b_only/NeuroMotion_B_Only_Probe.hex'
    if result.returncode or '0 Error(s), 0 Warning(s)' not in log.read_text(errors='replace') or not image.is_file():
        raise RuntimeError('B-only build failed; inspect build.log')
    for rel, digest in baseline.items():
        assert hashlib.sha256((src / rel).read_bytes()).hexdigest() == digest
    manifest = {'status': 'built', 'image': str(image), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                'production_sources_unchanged': baseline, 'flashed': False, 'device': 'B only',
                'ready_mask_required': 2, 'TTL_capture_disabled': True, 'A_capture_disabled': True,
                'firmware_name': 'NeuroMotion-B-only-probe', 'diagnostic_only': True}
    (output / 'build_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
