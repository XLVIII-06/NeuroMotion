"""Generate and verify the V1.1 dual-module breadboard map. No hardware access."""
from pathlib import Path
import argparse
import html
import json
import math

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[2]
LEFT = ['RLDOUT', 'IN1N', 'IN1P', 'IN2N', 'IN2P', 'GPIO1', 'GPIO2', 'GND', '3V3']
RIGHT = ['DRDY', 'DOUT', 'SCLK', 'DIN', 'CS', 'CLK', 'START', 'PWDN', 'CLKSEL']
X = {c: 310+i*40+(80 if i >= 5 else 0) for i, c in enumerate('abcdefghij')}
COLORS = {'3V3': '#d13939', 'GND': '#343d4b', 'DIN': '#00889c', 'DOUT': '#ad6c00',
          'START': '#9e6600', 'SCLK': '#5450bd', 'CS': '#218351', 'PWDN': '#b14a8c',
          'DRDY': '#226bb4', 'TTL': '#d46415'}


def make_layout(profile='right'):
    if profile not in ('right', 'original'):
        raise ValueError('Unknown firmware pin profile')
    drdy_a, drdy_b, start = ('PB8', 'PB2', 'PB4') if profile == 'right' else ('PA0', 'PA1', 'PB8')
    pins = {}
    for board, first in [('A', 1), ('B', 15)]:
        pins[board] = {name: f'c{first+i}' for i, name in enumerate(LEFT)}
        pins[board].update({name: f'g{first+i}' for i, name in enumerate(RIGHT)})
    wires = []
    def wire(stage, name, a, b):
        wires.append(dict(id=f'w{len(wires)+1}', stage=stage, name=name, a=a, b=b))
    for name, a, b in [('3V3', 'PSU_3V3', 'a11'), ('GND', 'PSU_GND', 'a13'),
                        ('GND', 'MCU_GND', 'b13'), ('DIN', 'PA7', 'f11'),
                        ('DOUT', 'PA6', 'f12'), ('START', start, 'f13'),
                        ('TTL', 'PA8', 'f25'), ('TTL', 'PB10', 'g25')]:
        wire('common', name, a, b)
    for board, row, clk, cs, pwdn, drdy, buscol, powercol, groundcol in [
            ('A', 1, 'PA5', 'PB9', 'PC8', drdy_a, 'g', 'b', 'c'),
            ('B', 15, 'PB3', 'PB6', 'PC9', drdy_b, 'h', 'c', 'd')]:
        for name, a, b in [('3V3', powercol+'11', f'a{row+8}'),
                            ('GND', groundcol+'13', f'a{row+7}'),
                            ('DRDY', drdy, f'j{row}'), ('DOUT', buscol+'12', f'j{row+1}'),
                            ('SCLK', clk, f'j{row+2}'), ('DIN', buscol+'11', f'j{row+3}'),
                            ('CS', cs, f'j{row+4}'), ('START', buscol+'13', f'j{row+6}'),
                            ('PWDN', pwdn, f'j{row+7}')]:
            wire(board, name, a, b)
    return {'version': 2, 'profile': profile,
            'firmware_target': 'NeuroMotion_4CH_Right' if profile == 'right' else 'NeuroMotion_4CH',
            'mcu': {'drdy_a': drdy_a, 'drdy_b': drdy_b, 'start': start}, 'pins': pins, 'wires': wires,
            'mapping': {'A': 'upper module, rows1-9', 'B': 'lower suspect module, rows15-23; isolate'},
            'physical_pin_order_user_confirmed': True, 'old_amplitude_network_removed_user_confirmed': True}


def verify(data):
    def group(hole):
        if len(hole) >= 2 and hole[0] in X and hole[1:].isdigit():
            row = int(hole[1:]); assert 1 <= row <= 30
            return ('L' if hole[0] <= 'e' else 'R')+str(row)
        return hole
    module_holes = [h for p in data['pins'].values() for h in p.values()]
    external = [h for w in data['wires'] for h in [w['a'],w['b']] if not (h[0] in X and h[1:].isdigit())]
    assert len(external) == len(set(external)), 'Duplicate external connector insertion'
    assert len(set(module_holes)) == 36
    assert math.isclose(2*2.54+7.62+2.54, 15.24)  # c-d-e-f-g
    for mode in ['placement', 'A', 'dual']:
        selected = [w for w in data['wires'] if mode == 'dual' or (mode == 'A' and w['stage'] != 'B')]
        occupied = list(module_holes)
        parent = {}
        def find(node):
            parent.setdefault(node, node)
            if parent[node] != node:
                parent[node] = find(parent[node])
            return parent[node]
        def join(a, b):
            parent[find(group(a))] = find(group(b))
        for w in selected:
            for h in [w['a'], w['b']]:
                if h[0] in X and h[1:].isdigit():
                    assert h not in occupied, f'Double insertion: {h}'
                    occupied.append(h)
            join(w['a'], w['b'])
        assert find(group('a11')) != find(group('a13'))
        assert find('PA5') != find('PB3')
        for board in ['A', 'B']:
            enabled = mode == 'dual' or (mode == 'A' and board == 'A')
            for name, hole in data['pins'][board].items():
                net = find(group(hole))
                if not enabled:
                    assert net not in {find(node) for node in external}
                elif name in ['RLDOUT', 'IN1N', 'IN1P', 'IN2N', 'IN2P', 'GPIO1', 'GPIO2', 'CLK', 'CLKSEL']:
                    assert not any(group(hole) in [group(w['a']), group(w['b'])] for w in selected)
            if enabled:
                expected = {'3V3': 'PSU_3V3', 'GND': 'PSU_GND', 'DIN': 'PA7', 'DOUT': 'PA6', 'START': data['mcu']['start'],
                            'SCLK': 'PA5' if board == 'A' else 'PB3', 'CS': 'PB9' if board == 'A' else 'PB6',
                            'PWDN': 'PC8' if board == 'A' else 'PC9', 'DRDY': data['mcu']['drdy_a' if board == 'A' else 'drdy_b']}
                for name, node in expected.items():
                    assert find(group(data['pins'][board][name])) == find(node), (mode, board, name)
        if mode != 'placement':
            assert find('PA8') == find('PB10')
            assert find('PA8') != find('PSU_GND')
            assert find('MCU_GND') == find('PSU_GND')
        for h in ['e11', 'e13', 'b8', 'b9', 'b22', 'b23']:
            assert h not in occupied
    return {'status': 'passed', 'module_pins': 36, 'wires_in_full_reference': len(data['wires']),
            'checks': ['polarity and port mapping', 'no hole double insertion', 'B isolated in placement and A-only modes',
                       'distinct left/right groups', 'separate SCLK', 'NC and analog pins disconnected', 'measurement holes free'],
            'hardware_powered_test_performed': False}


def picture(data):
    width, height = 1480, 1190
    im = Image.new('RGB', (width, height), '#f4f7fb'); d = ImageDraw.Draw(im)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="双模块面包板布局"><rect width="1480" height="1190" fill="#f4f7fb"/>']
    fontfile = 'C:/Windows/Fonts/msyh.ttc'
    def text(x, y, value, size=17, color='#1b334b'):
        d.text((x, y), value, font=ImageFont.truetype(fontfile, size), fill=color)
        svg.append(f'<text x="{x}" y="{y+size}" font-family="Microsoft YaHei,sans-serif" font-size="{size}" fill="{color}">{html.escape(value)}</text>')
    def rect(x, y, w, h, color, border=None):
        d.rectangle((x,y,x+w,y+h), fill=color, outline=border)
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="{color}" stroke="{border or color}"/>')
    def circle(x, y, r=3, color='#738599'):
        d.ellipse((x-r,y-r,x+r,y+r), fill=color)
        svg.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')
    def ypos(row): return 140+(row-1)*28
    text(28,20,'NeuroMotion：模块迁移到面包板 · 新位置编号 A / B',29)
    text(28,65,'当前先断电定位；下方 B 为疑似烧损板，电源、地、MCU及模拟线全部不接。',20,'#ad3333')
    for col, x in X.items(): text(x-5,108,col,17)
    for row in range(1,31):
        y = ypos(row); text(270,y-10,str(row),14)
        left = '#ffe4e4' if row==11 else '#dce2e9' if row==13 else '#e7edf4'
        right = '#d6eef0' if row==11 else '#faedce' if row in (12,13) else '#ffead7' if row==25 else '#e7edf4'
        rect(X['a']-12,y-9,184,18,left); rect(X['f']-12,y-9,184,18,right)
        for x in X.values(): circle(x,y)
    for board, first in [('A',1),('B',15)]:
        top = ypos(first)-17
        rect(X['c']-20,top,X['g']-X['c']+40,8*28+34,'#e9f1eb' if board=='A' else '#f6e1e1','#7a8d81' if board=='A' else '#b64747')
        for i,(left,right) in enumerate(zip(LEFT,RIGHT)):
            row=first+i; yy=ypos(row)
            circle(X['c'],yy,5,'#d13939' if left=='3V3' else '#333d4b' if left=='GND' else '#b59b5b')
            circle(X['g'],yy,5,'#b59b5b')
            text(X['c']+14,yy-10,left,14,'#bd2727' if left=='3V3' else '#263f33')
            text(X['g']-79,yy-10,right,14)
        text(45,top+32,f'模块 {board}',25)
        text(45,top+76,'c列：左排',17);text(45,top+108,'g列：右排',17)
        text(45,top+150,'A：第1至9行' if board=='A' else 'B：第15至23行',18)
        text(45,top+187,'已确认V1.1针序' if board=='A' else '疑似烧损，隔离检查',17,'#24634c' if board=='A' else '#ad3333')
    # A simple right-hand pin/port reference stays readable in the static preview.
    text(855,130,'固件端口与模块接口（正常工作参考）',22)
    text(855,176,'信号             A外接孔 / MCU          B外接孔 / MCU',17)
    rows=[('DRDY',f"j1 / {data['mcu']['drdy_a']}",f"j15 / {data['mcu']['drdy_b']}"),('DOUT','j2 / PA6','j16 / PA6'),('SCLK','j3 / PA5','j17 / PB3'),
          ('DIN','j4 / PA7','j18 / PA7'),('CS','j5 / PB9','j19 / PB6'),('START',f"j7 / {data['mcu']['start']}",f"j21 / {data['mcu']['start']}"),
          ('PWDN','j8 / PC8','j22 / PC9'),('GND','a8 / 共地','a22 / 共地'),('3V3','a9 / 外部3.3V','a23 / 外部3.3V')]
    for i,(name,a,b) in enumerate(rows):
        yy=215+i*36;text(855,yy,name,18,COLORS.get(name,'#1b334b'));text(1000,yy,a,17);text(1225,yy,b,17)
    text(855,570,'电源脚重点核对',22,'#ad3333')
    text(855,612,'A：c8 = GND；c9 = 3V3',20)
    text(855,649,'B：c22 = GND；c23 = 3V3',20)
    text(855,692,'实际插线选外侧 a / j 孔，避免碰模块裸脚。',17)
    text(855,730,'公共排：左11行=+3.3V；左13行=GND',17)
    text(855,766,f"右11行=DIN；右12行=DOUT；右13行=START({data['mcu']['start']})",17)
    text(855,802,'右25行：PA8原始TTL分到PB10；LED仅留接口',17)
    text(855,850,'左、右半排隔着中沟，不会自动连通！',19,'#ad3333')
    text(855,892,'疑似烧损B：先仅测 b23 至 b22 的断电电阻。',17,'#ad3333')
    text(855,928,'本图不允许给疑似烧损板直接上电试机。',17,'#ad3333')
    text(28,1020,'旧1mV网络已移走：新模块板不与旧网络孔位混用。模拟输入、RLD、GPIO、CLK/CLKSEL本轮不接。',20)
    text(28,1061,'两排针 c 至 g 的实际间距为15.24mm；模块正面朝上，RLDOUT/DRDY在各模块最上行。',18)
    text(28,1102,'图中 A/B 是本轮上/下位置编号，不再追认旧板身份。外部3.3V与MCU只共地，不并接NUCLEO的3V3。',18)
    text(28,1141,f"必须使用固件目标 {data['firmware_target']}；不能新图配旧固件。当前全部断电。",18,'#ad3333')
    # Wire overlays are interactive only, default hidden. Use exact-hole markers.
    def is_hole(endpoint):
        return endpoint[0] in X and endpoint[1:].isdigit()
    def position(endpoint, other):
        if is_hole(endpoint):
            return X[endpoint[0]], ypos(int(endpoint[1:]))
        if endpoint == 'PSU_3V3': return 230, 420
        if endpoint == 'PSU_GND': return 230, 475
        if endpoint == 'MCU_GND': return 230, 500
        offset = -15 if endpoint == 'PA8' else 15 if endpoint == 'PB10' else 0
        return 840, ypos(int(other[1:]))+offset
    svg.append('<g id="wire-layer" style="display:none">')
    for i,w in enumerate(data['wires']):
        a,b=position(w['a'],w['b']),position(w['b'],w['a']);color=COLORS[w['name']]
        # An overview of relationships, not a requirement for physical routing.
        lane=790+(i%5)*10 if max(a[0],b[0])>550 else 235-(i%4)*12
        pts=[a,(lane,a[1]),(lane,b[1]),b]
        svg.append(f'<g class="wire" data-stage="{w["stage"]}" id="{w["id"]}"><title>{w["id"]} {w["name"]}: {w["a"]} 至 {w["b"]}</title><polyline points="'+ ' '.join(f'{x},{y}' for x,y in pts)+f'" fill="none" stroke="{color}" stroke-width="3"/>')
        for xx,yy in [a,b]:svg.append(f'<circle cx="{xx}" cy="{yy}" r="5" fill="{color}"/>')
        for endpoint, xy in [(w['a'], a), (w['b'], b)]:
            if not is_hole(endpoint):
                label = {'PSU_3V3': '外部 +3.3V', 'PSU_GND': '外部 GND', 'MCU_GND': 'NUCLEO GND'}.get(endpoint, endpoint)
                tx = 55 if xy[0] < 300 else 780
                svg.append(f'<text x="{tx}" y="{xy[1]-7}" font-family="Microsoft YaHei,sans-serif" font-size="14" fill="{color}">{label}</text>')
        svg.append('</g>')
    svg.append('</g></svg>')
    return ''.join(svg),im


def page(data,svg):
    pinrows=''.join(f'<tr><td>{i+1}</td><td>{left}</td><td>c{i+1} / a{i+1}</td><td>c{i+15} / a{i+15}</td><td>{right}</td><td>g{i+1} / j{i+1}</td><td>g{i+15} / j{i+15}</td></tr>' for i,(left,right) in enumerate(zip(LEFT,RIGHT)))
    wire_rows=''.join(f'<tr data-stage="{w["stage"]}" onclick="focusWire(\'{w["id"]}\')"><td>{w["id"]}</td><td>{w["stage"]}</td><td>{w["name"]}</td><td>{w["a"]}</td><td>{w["b"]}</td></tr>' for w in data['wires'])
    content = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NeuroMotion 模块面包板系统接线图</title><style>
body{font:16px/1.8 "Microsoft YaHei",sans-serif;max-width:1480px;margin:20px auto;padding:0 16px;background:#f4f7fb;color:#19344e}section{padding:20px;background:white;border:1px solid #d8e3ef;border-radius:10px;margin:16px 0}h1{font-size:28px}svg{width:100%;height:auto}button,select{font:inherit;padding:8px;margin:4px}strong,.warn{color:#aa3030}table{border-collapse:collapse;width:100%;margin:12px 0}td,th{border:1px solid #cfdbe8;padding:7px;text-align:left}tbody tr:hover{background:#fff1cb}code{background:#e9eff7;padding:2px 4px}.scroll{overflow:auto}.note{color:#526778}
</style><h1>NeuroMotion 模块迁移：系统孔位与接线图</h1><section><strong>当前模式：全部断电，只定位两块板。下方 B 是疑似烧损板，全部电源和信号线均不接。</strong>
<p>本轮名称：上方为A、下方为B，不与此前“原A/原B”身份混用。两板针序已由用户确认与V1.1资料一致；旧1mV电阻网络已移出此板。USB与外部电源都关闭后再搬板；模块正面朝上，左排顶针RLDOUT放c1/c15，右排顶针DRDY放g1/g15，向下逐行排列。PCB不必贴平面包板，不强压。</p>
<p>c至g跨距15.24mm，与本地尺寸图600mil一致；a–e同行相通，f–j同行相通，中沟两边不相通。模块会遮住部分孔，实际外接优先用a列和j列；图表同时列出模块针脚孔与外接孔。每孔只插一个端头。</p>
<label>查看模式 <select id="mode" onchange="modeChanged()"><option value="placement">0 定位隔离（当前）</option><option value="check">1 疑似B断电测量（所有线不接）</option><option value="A">2 A单板参考（B全部隔离）</option><option value="dual">3 双板正常工作参考（B须为合格/备用板）</option></select></label>
<button onclick="focusWire(null)">取消单线突出</button><p id="mode-note" class="warn">当前只摆板、全部断电；不要按参考表一次接齐疑似烧损B。</p><p id="wire-note">点击接线表某一行可突出显示。图线只表示两端关系，交叉处不连接；实际跳线沿板外侧整理。</p></section>
<section>'''+svg+'''</section><section><h2>模块针序：从顶端逐行往下</h2><div class="scroll"><table><tr><th>顺序</th><th>左排名称</th><th>A 模块孔 / 外接孔</th><th>B 模块孔 / 外接孔</th><th>右排名称</th><th>A 模块孔 / 外接孔</th><th>B 模块孔 / 外接孔</th></tr>'''+pinrows+'''</table></div>
<p><strong>电源不能反接：A的a8是GND、a9是3V3；B的a22是GND、a23是3V3。</strong>二者相邻，接前读丝印。供电来自外部稳压3.3V；NUCLEO的3V3不接本电源。模拟端、RLDOUT、GPIO1/2、CLK、CLKSEL本轮都不接，两块板CLK不能相连。</p></section>
<section><h2>系统接线表（按所选模式显示）</h2><p>PA/PB/PC为NUCLEO MCU端口名。电源排：a11=外部+3.3V，a13=外部GND，b13=MCU GND。右半排：f11=PA7/DIN，f12=PA6/DOUT，f13=PB8/START；右25行分配PA8原始TTL至PB10。左、右同号行不是同一节点。</p>
<div class="scroll"><table id="wires"><thead><tr><th>编号</th><th>阶段</th><th>信号</th><th>端点一</th><th>端点二</th></tr></thead><tbody>'''+wire_rows+'''</tbody></table></div>
<p>PA5=D13，PB3=D3，PA7=D11，PA6=D12，PA0=A0，PA1=A1，PA8=D7，PB10=D6。CS/PWDN/START以表中的MCU端口名为准，不能猜相邻排针。A/B串行SCLK分别为PA5/PB3，不短接；DIN、DOUT、START按指定分线排共享。</p>
<p>TTL留用原先验证过的LED限流/分压支路，可从空闲h25取原始TTL；本轮先不新接LED或BL分压。PB10必须接原始数字TTL，不能接0.8V分压节点。电阻测试网络需另用面包板，先不接任何EMG模拟输入。RLDOUT不并联。</p></section>
<section><h2>分步搬迁与检查</h2><ol>
<li><strong>当前只做：</strong>全部断电，清除本板遗留跳线、旧电源线和电阻；按c/g列装好两板。疑似B仅摆放，无任何外接线。</li>
<li><strong>疑似B：</strong>全部电源与信号线保持断开，先直流电压档确认b23对b22近0V，再用电阻档红b23（3V3）、黑b22（GND）记录初始和约15秒后读数，观察可见烧痕。没有短路不代表正常；普通电源不能直接作为疑似烧损板的试机电源。</li>
<li><strong>先验供电分线：</strong>模块A/B的电源和数字线均不接时，接PSU_3V3至a11、PSU_GND至a13，核对红e11黑e13确为正3.3V，再关闭电源。此步骤须在定位与B断电检查确认后进行。</li>
<li><strong>A单板：</strong>用模式2接A与公共线，B保持完全隔离。断电核线；恢复电源后复位，检查A的ID。四通道固件在B缺席时不启动采样，这是预期，不因此直接重烧固件。</li>
<li><strong>双板：</strong>只有B已被适当验证，或换上未受损并经单板检查的备份件后，才按模式3补接B。首次只检查数字采集、模块ID和错误计数，不接动物/模拟测试网络。</li>
<li>数字采集稳定后才恢复1mV网络及幅值验证。疑似烧损板若无合适限流条件，只保留隔离检查，不能通过普通3.3V电源直接试机。</li></ol>
<p>检查孔预留：电源排e11(+)/e13(GND)；A供电b9(+)/b8(GND)；B供电b23(+)/b22(GND)。这些孔在工作参考接线中也未被占用。本图不包含临时限流试验电路，不可据此给烧损板直接上电。</p>
<p class="note">依据：本地 ADS1292模块正面/尺寸图（V1.1）、当前FOUR_CHANNEL_GUIDE及dual_adc.c。布局与静态连通性已由生成脚本检查；实物上电和功能验证尚未完成。</p></section><script>
const wires='''+json.dumps(data['wires'],ensure_ascii=False)+''';let selected=null;
function allowed(w,m){return m==='dual'||(m==='A'&&w.stage!=='B');}
function focusWire(id){selected=id;render();}
function modeChanged(){selected=null;render();}
function render(){const m=document.getElementById('mode').value;document.getElementById('wire-layer').style.display=['A','dual'].includes(m)?'inline':'none';
const notes={placement:'当前只摆板、全部断电；模块A/B均不接线。',check:'疑似B只做断电检查：红b23、黑b22。所有外接线仍不接。',A:'A单板参考：B连电源、地和数字线都不接；先核线，不因切换模式就上电。',dual:'仅供合格/备份B的正常工作参考。当前疑似烧损B不能直接按此图接电试机。'};document.getElementById('mode-note').textContent=notes[m];
wires.forEach(w=>{const on=allowed(w,m),el=document.getElementById(w.id);el.style.display=on?'inline':'none';el.style.opacity=!selected||selected===w.id?'1':'.10';});
document.querySelectorAll('#wires tbody tr').forEach((tr,i)=>{tr.style.display=allowed(wires[i],m)?'table-row':'none';tr.style.background=selected===wires[i].id?'#fff0bd':'';});
const w=wires.find(w=>w.id===selected);document.getElementById('wire-note').textContent=w?`${w.id} ${w.stage} ${w.name}：${w.a} 至 ${w.b}`:'点击接线表某行可突出显示；交叉处不连接，实际跳线沿板外侧整理。';}
render();</script></html>'''
    notice = f"<section><strong>本图配套固件目标：{data['firmware_target']}。固件尚未下载时，不按新图上电验证。</strong>"
    if data['profile'] == 'right':
        notice += '''<p>右侧DRDY方案，仅移三根MCU端线：A的j1从PA0改PB8（CN10-3/D15）；B的j15从PA1改PB2（CN10-22）；START公共排f13从PB8改PB4（CN10-27/D5）。模块端和其余线路不变。PB8已成为DRDY输入，不能再接START。</p>'''
        content = content.replace('f13=PB8/START', 'f13=PB4/START')
        content = content.replace('PA0=A0，PA1=A1，', 'PB8=D15/CN10-3，PB2=CN10-22，PB4=D5/CN10-27，')
    content = content.replace('</h1>', '</h1>'+notice+'</section>', 1)
    return content


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=['right','original'],default='right')
    parser.add_argument('--stem',default='SYSTEM_MODULE_BREADBOARD')
    args=parser.parse_args()
    if Path(args.stem).name != args.stem:
        parser.error('stem must be a filename without directories')
    data=make_layout(args.profile);checks=verify(data);svg,im=picture(data)
    folder=ROOT/'Docs';folder.mkdir(exist_ok=True)
    (folder/(args.stem+'.svg')).write_text(svg,encoding='utf-8')
    im.save(folder/(args.stem+'.png'))
    (folder/(args.stem+'.html')).write_text(page(data,svg),encoding='utf-8')
    output=ROOT/'NeuroMotion_GUI/output/amplitude_screen';output.mkdir(parents=True,exist_ok=True)
    filename = 'module_board_layout_verified.json' if args.profile == 'right' else 'module_board_layout_original_verified.json'
    (output/filename).write_text(json.dumps({'layout':data,'validation':checks},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(checks,ensure_ascii=False))


if __name__=='__main__':main()
