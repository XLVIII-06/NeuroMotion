"""Build reviewed Keil targets; this command never programs a board."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess


def locate_uv4():
    found=shutil.which('UV4.exe')
    if found:return Path(found)
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE,winreg.HKEY_CURRENT_USER):
        for base in (r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
            try:
                with winreg.OpenKey(hive,base) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        with winreg.OpenKey(key,winreg.EnumKey(key,i)) as sub:
                            try:
                                name=winreg.QueryValueEx(sub,'DisplayName')[0]
                                uninstall=winreg.QueryValueEx(sub,'UninstallString')[0]
                            except OSError:continue
                            if 'Keil' in name:
                                candidate=Path(uninstall.strip('"')).parent/'UV4/UV4.exe'
                                if candidate.is_file():return candidate
            except OSError:continue
    return None


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--uv4',type=Path)
    p.add_argument('--target',choices=['all','legacy','four','right'],default='all');a=p.parse_args()
    exe=a.uv4 or locate_uv4()
    if exe is None or not exe.is_file():p.error('Keil UV4 not found; specify --uv4')
    base=Path(__file__).resolve().parent/'ads1292_f446re/MDK-ARM'
    output=base/'build';output.mkdir(exist_ok=True)
    targets=[('legacy','ads1292_f446re',base/'ads1292_f446re/ads1292_f446re.hex'),
             ('four','NeuroMotion_4CH',base/'build/four_channel/NeuroMotion_4CH.hex'),
             ('right','NeuroMotion_4CH_Right',base/'build/four_channel_right/NeuroMotion_4CH_Right.hex')]
    results=[]
    for kind,target,image in targets:
        if a.target not in ('all',kind):continue
        log=output/(kind+'_build.log')
        r=subprocess.run([str(exe),'-b',str(base/'ads1292_f446re.uvprojx'),'-t',target,'-j0','-o',str(log)],
                         timeout=150,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        text=log.read_text(errors='replace') if log.exists() else ''
        if r.returncode or '0 Error(s), 0 Warning(s)' not in text or not image.is_file():
            raise RuntimeError(f'Firmware build failed: {target}; see {log}')
        results.append({'target':target,'image':str(image),'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'size_bytes':image.stat().st_size,'log':str(log)})
    (output/'firmware_manifest.json').write_text(json.dumps({'status':'passed','images':results,'flashed':False},indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2))


if __name__=='__main__':main()
