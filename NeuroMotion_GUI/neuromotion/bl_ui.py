"""Independent BL preview and paper-parameter editing, outside the acquisition worker."""
import csv
from datetime import datetime
import math
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

from .bl422 import load_bl422
from .paths import app_root
from .plotting import TraceCanvas
from .stimulus_sync import analog_ttl, protocol


def preview_bl(path, parameters=None, cancel_event=None):
    with load_bl422(path, parameters_path=parameters, cancel_event=cancel_event) as recording:
        result = recording.summary()
        selected = np.arange(0, len(recording.samples), max(1, len(recording.samples)//1800))
        result['preview_time_s'] = (recording.samples['time_us'][selected]/1e6).tolist()
        result['preview_uv'] = (recording.samples['ttl_mv'][selected]*1000).tolist()
        try:
            trace, voltage = analog_ttl(recording)
            markers = protocol(trace)
            result['ttl_summary'] = {'edges': len(trace.times), 'start_s': markers['start_s'],
                                     'end_s': markers['end_s'], 'inverted': markers['inverted'],
                                     'low_mv': voltage['low_mv'], 'high_mv': voltage['high_mv']}
        except ValueError as exc:
            result['sync_issue'] = str(exc)
        return {'status': 'bl_preview_ready', 'summary': result}


class BLPage(ttk.Frame):
    def __init__(self, parent, host):
        super().__init__(parent, padding=12)
        self.host = host
        self.events = []
        self.summary = tk.StringVar(value='导入第二通道 TTL 和单脉冲开始标注；电流为设定值 mA。')
        self.width = tk.StringVar()
        self.notes = tk.StringVar()
        self.columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate([('BL TTL / 事件 CSV', host.bl_csv), ('纸面参数表（可选）', host.stimulus_parameters)]):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky='w', padx=5)
            ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky='ew', pady=4)
            host._button(self, '选择 CSV', lambda v=variable: self._browse(v)).grid(row=row, column=2, padx=5)
        host._button(self, '读取并预览', self.load, primary=True).grid(row=2, column=0, pady=6)
        host._button(self, '保存参数表', self.save).grid(row=2, column=2)
        ttk.Label(self, textvariable=self.summary, wraplength=1100).grid(row=3, column=0, columnspan=3, sticky='w', pady=5)
        self.plot = TraceCanvas(self, 'BL TTL', '#0f9e82')
        self.plot.grid(row=4, column=0, columnspan=3, sticky='ew')
        frame = ttk.Frame(self); frame.grid(row=5, column=0, columnspan=3, sticky='nsew', pady=8)
        self.rowconfigure(5, weight=1)
        columns = ('time', 'current', 'width', 'notes', 'raw')
        self.table = ttk.Treeview(frame, columns=columns, show='headings', height=6)
        for key, name, width in zip(columns, ['BL 时间 s', '设定电流 mA', '脉宽 ms', '纸面备注', 'CSV 原文'], [110, 130, 100, 200, 280]):
            self.table.heading(key, text=name); self.table.column(key, width=width)
        scroll = ttk.Scrollbar(frame, command=self.table.yview); self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y'); self.table.pack(fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self._selected)
        editor = ttk.Frame(self); editor.grid(row=6, column=0, columnspan=3, sticky='ew')
        ttk.Label(editor, text='脉宽 ms（空白＝未知）').pack(side='left')
        ttk.Entry(editor, textvariable=self.width, width=10).pack(side='left', padx=5)
        ttk.Label(editor, text='备注').pack(side='left')
        ttk.Entry(editor, textvariable=self.notes, width=24).pack(side='left', padx=5)
        host._button(editor, '更新选中事件', self.apply_selected).pack(side='left', padx=4)
        host._button(editor, '脉宽应用全部', self.apply_all).pack(side='left')
        ttk.Label(self, text='事件时间仍是软件标注，未校准实际刺激输出延迟。保存参数后可在“时间对齐”中复用。', foreground='#64748b').grid(row=7, column=0, columnspan=3, sticky='w', pady=6)

    def _browse(self, variable):
        path = filedialog.askopenfilename(parent=self, filetypes=[('CSV', '*.csv')])
        if path:
            variable.set(path)

    def load(self):
        path, params = self.host.bl_csv.get().strip(), self.host.stimulus_parameters.get().strip()
        if not path:
            messagebox.showerror('缺少 BL 文件', '请选择 BL 第二通道 TTL / 事件 CSV。', parent=self); return
        self.host._background('正在读取 BL TTL 和刺激标注…',
                              lambda: preview_bl(path, params or None, self.host.cancel_event), cancellable=True)

    def show_summary(self, summary):
        self.events = summary['events']
        voltage = summary.get('ttl_summary', {})
        self.summary.set(f"{summary['sample_count']} 点 · {summary['sample_rate_hz']:g} Hz · {len(self.events)} 条标注 · "
                         + (f"识别 {voltage['edges']} 个边沿，起始 {voltage['start_s']:.4f} s" if voltage else '同步待检查：'+summary.get('sync_issue', '')))
        self._fill_table()
        times = summary['preview_time_s']
        self.plot.draw(times, summary['preview_uv'], x_limits=(times[0], times[-1]), unit='mV')
        self.host.book.select(self)

    def _fill_table(self):
        self.table.delete(*self.table.get_children())
        for e in self.events:
            self.table.insert('', 'end', iid=e['event_id'], values=[f"{e['bl_time_s']:.3f}",
                              e['commanded_current_ma'] if e['commanded_current_ma'] is not None else '未解析',
                              e['pulse_width_ms'] if e['pulse_width_ms'] is not None else '未知', e['notes'], e['raw_event_text']])

    def _selected(self, _=None):
        chosen = self.table.selection()
        if chosen:
            e = next(e for e in self.events if e['event_id'] == chosen[0])
            self.width.set('' if e['pulse_width_ms'] is None else str(e['pulse_width_ms']))
            self.notes.set(e['notes'])

    def _width(self):
        text = self.width.get().strip()
        value = float(text) if text else None
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError('脉宽必须为正数，未知时留空。')
        return value

    def apply_selected(self):
        try:
            chosen = self.table.selection()
            if chosen:
                e = next(e for e in self.events if e['event_id'] == chosen[0])
                e.update(pulse_width_ms=self._width(), notes=self.notes.get())
                self._fill_table()
        except ValueError as exc:
            messagebox.showerror('参数无效', str(exc), parent=self)

    def apply_all(self):
        try:
            value = self._width()
            for e in self.events:
                e['pulse_width_ms'] = value
            self._fill_table()
        except ValueError as exc:
            messagebox.showerror('参数无效', str(exc), parent=self)

    def save(self):
        if not self.events:
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv', initialdir=str(app_root()/'output'),
                                          initialfile=f'stimulus_parameters_{datetime.now():%Y%m%d_%H%M%S}.csv')
        if not path:
            return
        try:
            with Path(path).open('x', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['event_id', 'pulse_width_ms', 'notes'], extrasaction='ignore')
                writer.writeheader(); writer.writerows(self.events)
            self.host.stimulus_parameters.set(path)
            self.host._log(f'纸面参数表已保存：{path}')
        except OSError as exc:
            messagebox.showerror('保存失败', f'{exc}\n请选择新的参数表文件名。', parent=self)
