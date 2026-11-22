import os, pathlib, re, shutil, subprocess

ROOT=pathlib.Path(__file__).resolve().parent
RUNTIME=ROOT/'adb-runtime/usr'

class NoIndicatorError(RuntimeError): pass

def adb(*args):
    binary=shutil.which('adb') or str(RUNTIME/'bin/adb')
    env=os.environ.copy()
    if binary.startswith(str(RUNTIME)):
        env['LD_LIBRARY_PATH']=f'{RUNTIME}/lib/aarch64-linux-gnu/android:{RUNTIME}/lib/aarch64-linux-gnu'
    return subprocess.run([binary,'-P','5037',*args],env=env,text=True,capture_output=True,check=True,timeout=15).stdout

def transport(device):
    for line in adb('devices','-l').splitlines():
        if re.search(r'\busb:'+re.escape(device)+r'(?=\s|$)',line):
            found=re.search(r'\btransport_id:(\d+)\b',line)
            if found and re.search(r'\bdevice\b',line): return found.group(1)
    raise RuntimeError('设备没有可用的 ADB 接口')

def shell(device,command):
    output=adb('-t',transport(device),'shell','set -e; '+command+'; echo ZXW_LED_OK')
    if 'ZXW_LED_OK' not in output: raise RuntimeError('指示灯操作失败')
    return output

def snapshot(device):
    output=shell(device,'''set +e; for d in /sys/class/leds/*; do
[ -r "$d/brightness" ] || continue
printf "%s|" "${d##*/}"; cat "$d/brightness" | tr -d '\\n'; printf "|"; cat "$d/trigger" | tr -d '\\n'; printf "|"; if [ -f "$d/delay_on" ]; then cat "$d/delay_on" | tr -d '\\n'; fi; printf "|"; if [ -f "$d/delay_off" ]; then cat "$d/delay_off" | tr -d '\\n'; fi; echo
done''')
    result=[]
    for line in output.splitlines():
        parts=line.strip().split('|')
        if len(parts)!=5 or not re.fullmatch(r'[a-zA-Z0-9_-]+',parts[0]) or not parts[1].isdigit(): continue
        active=re.search(r'\[([a-zA-Z0-9_-]+)\]',parts[2])
        result.append({'name':parts[0],'brightness':int(parts[1]),'trigger':active.group(1) if active else 'none','on':int(parts[3]) if parts[3].isdigit() else 0,'off':int(parts[4]) if parts[4].isdigit() else 0})
    if not result: raise NoIndicatorError('设备没有可用的指示灯节点')
    return result

def off(device):
    shell(device,'for d in /sys/class/leds/*; do [ ! -w "$d/brightness" ] || echo 0 > "$d/brightness"; done')

def restore(device,items):
    commands=[]
    for led in items:
        name=led['name']; trigger=led['trigger']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',name) or not re.fullmatch(r'[a-zA-Z0-9_-]+',trigger): raise ValueError('指示灯记录无效')
        path=f'/sys/class/leds/{name}'
        commands.append(f'echo {int(led["brightness"])} > {path}/brightness')
        if trigger!='none': commands.append(f'echo {trigger} > {path}/trigger')
        if trigger=='timer': commands.append(f'echo {int(led["on"])} > {path}/delay_on; echo {int(led["off"])} > {path}/delay_off')
    shell(device,'; '.join(commands))
