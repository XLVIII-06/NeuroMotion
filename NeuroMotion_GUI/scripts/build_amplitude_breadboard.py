"""Generate and electrically check the spacious, independent amplitude-test layout."""
from pathlib import Path
import json
from collections import Counter
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PARTS = [
    ('R6', 10000, 'a1', 'a11', 'VCC', 'VM'),
    ('R7', 10000, 'e3', 'e13', 'VM', 'GND'),
    ('R1', 100000, 'c2', 'c12', 'INPUT', 'J'),
    ('R2', 100000, 'd5', 'd15', 'J', 'X'),
    ('R3', 10000, 'a20', 'a30', 'X', 'VM'),
    ('R4', 100000, 'e19', 'e29', 'X', 'SIG'),
    ('R5a', 465, 'b18', 'b28', 'VM', 'M'),
    ('R5b', 213, 'd17', 'd27', 'M', 'SIG'),
]
WIRES = [
    ('VM', 'b11', 'b3'), ('VM', 'c3', 'c18'), ('VM', 'd18', 'b30'),
    ('J', 'b12', 'b5'), ('X', 'c15', 'c20'), ('X', 'd20', 'c19'),
    ('SIG', 'b29', 'b27'), ('M', 'c28', 'c17'), ('SIG', 'c29', 'c16'),
]
PORTS = {'VM': 'c11', 'SIG': 'c27', 'GND': 'c13', 'VCC': 'c1', 'INPUT': 'b2', 'X': 'b15'}
COLORS = {'VM': '#2563eb', 'SIG': '#098563', 'GND': '#475569', 'VCC': '#d83342',
          'INPUT': '#bc6200', 'X': '#8b4cc0', 'J': '#916445', 'M': '#916445'}


def validate():
    for _, _, a, b, _, _ in PARTS:
        assert a[0] == b[0] and int(b[1:])-int(a[1:]) == 10
    for h in [p for r in PARTS for p in r[2:4]]+[p for w in WIRES for p in w[1:]]+list(PORTS.values()):
        assert h[0] in 'abcde' and 1 <= int(h[1:]) <= 30
    for i, left in enumerate(PARTS):
        for right in PARTS[i+1:]:
            if left[2][0] == right[2][0]:
                assert max(int(left[2][1:]),int(right[2][1:])) > min(int(left[3][1:]),int(right[3][1:]))
    def group(h):
        return ('L' if h[0] <= 'e' else 'R')+h[1:]
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]
    for _, a, b in WIRES:
        parent[find(group(a))] = find(group(b))
    labels = {}
    endpoints = []
    for _, _, a, b, na, nb in PARTS:
        for h, node in [(a, na), (b, nb)]:
            endpoints.append(h)
            key = find(group(h))
            assert key not in labels or labels[key] == node, (h, node, labels)
            labels[key] = node
    assert len(labels) == 8, 'Different electrical nodes were shorted'
    for node, a, b in WIRES:
        assert labels[find(group(a))] == labels[find(group(b))] == node
    for node, holes in [('VM', ['c30','d30','e30','a18']), ('SIG', ['a16','b16','d16','e16']),
                        ('VCC', ['b1','e1']), ('GND', ['b13','a13']), ('INPUT', ['a2'])]:
        assert all(labels[find(group(h))] == node for h in holes)
    assert len({find(group(p)) for p in PORTS.values()}) == 6
    for node, hole in PORTS.items():
        assert labels[find(group(hole))] == node
    endpoints += [h for _, a, b in WIRES for h in (a, b)]
    endpoints += ['b1', 'b13', 'a2']  # reserved power/input sockets
    endpoints += ['c30', 'd30', 'e30', 'a18', 'a16', 'b16', 'd16', 'e16']
    assert all(n == 1 for n in Counter(endpoints).values()), 'Two leads occupy one hole'
    assert not set(PORTS.values()) & set(endpoints), 'Measurement socket is occupied'
    unknown = ['VM', 'J', 'X', 'SIG', 'M']
    results = []
    for input_v in (0, 3.32):
        known = {'VCC': 3.32, 'GND': 0, 'INPUT': input_v}
        matrix = np.zeros((5, 5)); rhs = np.zeros(5)
        for _, ohms, _, _, left, right in PARTS:
            for a, b in ((left, right), (right, left)):
                if a not in unknown:
                    continue
                i = unknown.index(a); matrix[i, i] += 1/ohms
                if b in unknown:
                    matrix[i, unknown.index(b)] -= 1/ohms
                else:
                    rhs[i] += known[b]/ohms
        v = dict(zip(unknown, np.linalg.solve(matrix, rhs)))
        results.append({'vm_v': float(v['VM']), 'sig_v': float(v['SIG']),
                        'diff_mv': float((v['SIG']-v['VM'])*1000)})
    vpp = results[1]['diff_mv']-results[0]['diff_mv']
    assert .94 < vpp < .96
    assert abs(results[1]['diff_mv']+results[0]['diff_mv']) < 1e-8
    return {'status': 'topology_verified', 'physical_validated': False, 'external_supply_v': 3.32,
            'new_resistors_user_measured_ohms': [465, 213], 'other_resistors': 'nominal, not remeasured',
            'static_calculation': results, 'vpp_mv': vpp,
            'unpowered_vm_sig_ohms': 678*110000/(678+110000),
            'parts': PARTS, 'wires': WIRES, 'measurement_ports': PORTS}


def render_preview():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei']
    fig, ax = plt.subplots(figsize=(10, 11.5))
    ax.set(xlim=(-1, 8), ylim=(33, -2)); ax.axis('off')
    for r in range(1, 31):
        ax.plot([0, 4], [r, r], color='#e1e9f0', linewidth=8, zorder=0)
        ax.scatter(range(5), [r]*5, s=7, color='#6c7e90')
        ax.text(-.5, r, str(r), va='center', fontsize=8)
    for i, c in enumerate('abcde'): ax.text(i, -.2, c, ha='center', fontsize=12)
    for i, (name, value, a, b, _, _) in enumerate(PARTS):
        x = 'abcde'.index(a[0]); u, v = int(a[1:]), int(b[1:]); mid = (u+v)/2
        ax.plot([x, x], [u, v], color='#3a5877', linewidth=2)
        ax.add_patch(Rectangle((x-.17, mid-1.4), .34, 2.8, facecolor='#ffdaad' if name.startswith('R5') else '#bbdaef', edgecolor='#3a5877', zorder=3))
        ax.text(x, mid, name, rotation=90, va='center', ha='center', fontsize=9, zorder=4)
        label = '470Ω (实测465Ω)' if name == 'R5a' else '220Ω (实测213Ω)' if name == 'R5b' else f'{value/1000:g}kΩ'
        ax.text(4.8, 2+i*2, f'{name}  {label}\n{a} - {b}', fontsize=10)
    for name, h in PORTS.items():
        ax.scatter(['abcde'.index(h[0])], [int(h[1:])], s=100, facecolors='none', edgecolors=COLORS[name], linewidths=2, zorder=5)
    ax.text(4.8, 20, '测量：c11 - c27\n预计约 674Ω\n先保持断电', fontsize=12, color='#a74916')
    ax.text(-.6, -1.4, 'v3 · a–e / 1–30 行 · 纵向跨10行；跳线步骤请看HTML', fontsize=13)
    ax.text(-.5, 32, '同一行五孔连通；跨过的中间孔不连接。图片只显示电阻，另需九根内部跳线。', fontsize=9)
    fig.savefig(ROOT/'Docs/AMPLITUDE_1MV_BREADBOARD.png', dpi=140, bbox_inches='tight')
    plt.close(fig)


def main():
    from html import escape
    evidence = validate()
    for node, a, b in WIRES:
        assert a != b
    x = lambda c: 180+'abcde'.index(c)*70
    y = lambda r: 80+r*24
    def hole(h): return x(h[0]), y(int(h[1:]))
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 910" aria-label="a-e第1至30行纵向电阻孔位图">',
           '<rect width="900" height="910" fill="#f8fafc"/>', '<g font-family="Microsoft YaHei,sans-serif">',
           '<text x="35" y="35" font-size="21">v3：仅 a–e，1–30 行；八只电阻均纵向跨 10 行</text>']
    for c in 'abcde':
        svg.append(f'<text x="{x(c)-5}" y="75" font-size="17">{c}</text>')
    for r in range(1,31):
        svg.append(f'<rect x="165" y="{y(r)-9}" width="310" height="18" rx="6" fill="#e3eaf2"/>')
        svg.append(f'<text x="130" y="{y(r)+5}" font-size="13">{r}</text>')
        for c in 'abcde':
            svg.append(f'<circle cx="{x(c)}" cy="{y(r)}" r="3" fill="#6d7f93"/>')
    for name,v,a,b,_,_ in PARTS:
        ax,ay=hole(a);bx,by=hole(b);mid=(ay+by)/2
        label=name+' '+('470Ω' if name=='R5a' else '220Ω' if name=='R5b' else str(int(v/1000))+'kΩ')
        svg += [f'<line x1="{ax}" y1="{ay}" x2="{bx}" y2="{by}" stroke="#435e7a" stroke-width="3"/>',
                f'<rect x="{ax-13}" y="{mid-44}" width="26" height="88" rx="7" fill="{ "#ffdcad" if name.startswith("R5") else "#bbd9ee"}" stroke="#435e7a"/>',
                f'<text x="{ax}" y="{mid+4}" transform="rotate(-90 {ax} {mid})" text-anchor="middle" font-size="13">{label}</text>']
    for i,(node,a,b) in enumerate(WIRES):
        ax,ay=hole(a);bx,by=hole(b);lane=560+i*28;color=COLORS[node]
        svg.append(f'<g class="wire" id="w{i}" style="display:none"><path d="M{ax} {ay} H{lane} V{by} H{bx}" fill="none" stroke="{color}" stroke-width="3" stroke-dasharray="7 4"/><circle cx="{ax}" cy="{ay}" r="6" fill="{color}"/><circle cx="{bx}" cy="{by}" r="6" fill="{color}"/></g>')
    for node,h in PORTS.items():
        hx,hy=hole(h);svg.append(f'<circle cx="{hx}" cy="{hy}" r="8" fill="none" stroke="{COLORS[node]}" stroke-width="2"/>')
    for i,(name,v,a,b,_,_) in enumerate(PARTS):
        svg.append(f'<text x="535" y="{115+i*38}" font-size="16">{escape(name)}：{a} ↔ {b}</text>')
    svg += ['<text x="535" y="455" font-size="16">空闲测量孔</text>']
    for i,(node,h) in enumerate(PORTS.items()):
        svg.append(f'<text x="535" y="{485+i*28}" fill="{COLORS[node]}" font-size="15">{node}：{h}</text>')
    svg += ['<text x="35" y="855" font-size="16">每一行 a–e 内部连通；不同行不连通。不使用 f–j 或侧边电源轨。</text>',
            '<text x="35" y="885" font-size="15">横向为阅读而放大；跨过的孔不连接，元件只接触两端。每孔一个线端。</text></g></svg>']
    graphic=''.join(svg)
    (ROOT/'Docs/AMPLITUDE_1MV_BREADBOARD.svg').write_text(graphic,encoding='utf-8')
    parts=''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in [name, '470Ω（实测465Ω）' if name=='R5a' else '220Ω（实测213Ω）' if name=='R5b' else str(int(value/1000))+'kΩ', a,b])+'</tr>' for name,value,a,b,_,_ in PARTS)
    wires=''.join(f'<tr onclick="show({i})"><td>{i+1} · {node}</td><td>{a}</td><td>{b}</td></tr>' for i,(node,a,b) in enumerate(WIRES))
    data=json.dumps(WIRES,ensure_ascii=False)
    page='<!doctype html><html lang="zh"><meta charset="utf-8"><title>NeuroMotion 纵向幅值网络 v3</title><style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;background:#f3f6fa;color:#18314a;max-width:1050px;margin:24px auto;padding:0 16px}section{background:white;padding:20px;margin:15px 0;border:1px solid #d6e0ec;border-radius:10px}h1{font-size:27px}strong{color:#aa4418}svg{width:100%;height:auto}table{border-collapse:collapse;width:100%}th,td{border:1px solid #d6e0ec;padding:7px;text-align:left}button,select{font:inherit;padding:6px}tbody tr{cursor:pointer}tbody tr:hover{background:#fff1cd}.selected{background:#ffe7ac}code{background:#e9eff7;padding:2px 4px}</style><h1>约 1 mVpp：纵向宽间距重搭版 v3</h1><section><strong>本页完全替代旧孔位：只用 a–e、1–30 行，先保持全部断电。</strong><p>旧测试网络元件、跳线取下；MCU/ADS 数字线、原 PA8→PB10 和 LED 支路保留。新网络的外部电源线、PA8输入线和八根 ADS 模拟线均先不接，动物及 RLD 电极断开。</p><p>八只电阻的两端都在同一列，相差 10 行；标准 2.54mm 孔距对应约 25.4mm。图中电阻体沿列纵向放置。同列元件的跨度不重叠，相邻电阻体也错开。不强拉引脚，不把电阻体强压到板面；若实物引脚长度不足以跨这些孔，先停止并反馈。</p><p>同一行 a–e 内部导通，不同行不导通。长引脚经过的中间孔没有电气连接；裸脚不得碰到其他裸脚或跳线端头。跳线使用绝缘线，绕开电阻体。</p></section><section><h2>先摆八只电阻，再接九根跳线</h2><button onclick="show(-1)">只看电阻</button> <select id="select" onchange="show(Number(this.value))"><option value="-1">逐根查看跳线</option></select><p id="detail">默认显示电阻；点击跳线表格可突出显示一根跳线，交叉处没有连接。</p>GRAPHIC<h3>电阻孔位</h3><table><tr><th>编号</th><th>阻值</th><th>上端</th><th>下端</th></tr>PARTS</table><h3>九根内部跳线（逐根连接）</h3><table id="wires"><thead><tr><th>序号 / 节点</th><th>起点</th><th>终点</th></tr></thead><tbody>WIRES</tbody></table></section><section><h2>本轮只做断电检查</h2><p>所有外部连线暂留空，不接高低电平跳线。万用表电阻档，黑表笔 COM、红表笔 V/Ω，测 <strong>c11（VM）↔c27（SIG）</strong>，预计约 <strong>674Ω</strong>。测完保持断电并报告读数。</p><p>470Ω 实测465Ω，220Ω 实测213Ω，已代入计算；其余六只用名义值，3.32V 输入预计约0.950mVpp。DT9205A最小显示0.1mV，后续仅作量级粗测，不把名义分压当测量真值。</p><h3>后续接口预留：现在全部不接</h3><p>外部正电源 b1，共地 b13；输入 a2。测量孔：VCC c1、GND c13、INPUT b2、VM c11、SIG c27、X b15。</p><p>未来四路 N 接 c30/d30/e30/a18，四路 P 接 a16/b16/d16/e16，顺序均为 A1/A2/B1/B2。当前不插这八根线。以后低电平 a2→a13，高电平必须撤低后改 a2→e1；PA8 动态输入也只能在撤掉静态跳线后接 a2。</p></section><script>const wires=DATA;function show(n){document.querySelectorAll(\'.wire\').forEach((w,i)=>w.style.display=i===n?\'inline\':\'none\');document.querySelectorAll(\'#wires tbody tr\').forEach((w,i)=>w.classList.toggle(\'selected\',i===n));document.getElementById(\'select\').value=n;document.getElementById(\'detail\').textContent=n<0?\'仅显示电阻；外部供电、PA8、ADS保持断开。\':`${n+1} · ${wires[n][0]}：${wires[n][1]} → ${wires[n][2]}`;}wires.forEach(([node,a,b],i)=>document.getElementById(\'select\').add(new Option(`${i+1} ${node}: ${a}→${b}`,i)));</script></html>'
    page=page.replace('GRAPHIC',graphic).replace('PARTS',parts).replace('WIRES',wires).replace('DATA',data)
    (ROOT/'Docs/AMPLITUDE_1MV_BREADBOARD.html').write_text(page,encoding='utf-8')
    path=ROOT/'NeuroMotion_GUI/output/amplitude_screen/layout_v3_verified.json'
    path.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    render_preview()
    print('Verified v3: a-e rows1-30, eight longitudinal resistors, nine wires, unique sockets; expected',evidence['unpowered_vm_sig_ohms'],'ohms')


if __name__ == '__main__': main()
