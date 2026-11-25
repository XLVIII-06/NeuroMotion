import csv
from pathlib import Path
import tempfile
import unittest

import numpy as np

from neuromotion.bl422 import BLInputError, load_bl422, parse_time_us, export_parameter_template, apply_parameters


def make_bl(path, *, rows=100, encoding='gb18030', change=None):
    with Path(path).open('w', encoding=encoding, newline='') as stream:
        stream.write(f'导出数据的原始文件:synthetic.tmen\nTTL，总共{rows}个数据点，采样率：1000Hz\n')
        writer = csv.writer(stream)
        writer.writerow(['序号', '时间', '2通道数据(mV)', '事件'])
        for i in range(rows):
            row = [i+1, f'00:{i/1000:06.3f}', ' 16.52 ',
                   'Stimulate: -0.50mA' if i == 10 else 'Stimulate: 0.00mA' if i == 20 else '人工备注' if i == 30 else '']
            if change:
                row = change(i, row)
            writer.writerow(row)


class BLImportTests(unittest.TestCase):
    def test_encoding_units_signed_zero_and_unknown_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            for encoding in ('gb18030', 'utf-8-sig'):
                path = Path(tmp)/f'{encoding}.csv'
                make_bl(path, encoding=encoding)
                with load_bl422(path) as record:
                    self.assertIsInstance(record.samples, np.memmap)
                    self.assertEqual(record.samples['time_us'].tolist(), list(range(0, 100000, 1000)))
                    self.assertEqual(record.metadata['voltage_unit'], 'mV')
                    self.assertEqual(len(record.events), 3)
                    self.assertEqual([e['commanded_current_ma'] for e in record.events], [-.5, 0., None])
                    self.assertEqual(record.events[0]['source_sequence'], 11)
                    self.assertEqual(record.events[0]['bl_time_s'], .01)
                    self.assertEqual(record.events[-1]['raw_event_text'], '人工备注')
                    self.assertIsNone(record.events[0]['pulse_width_ms'])
                    self.assertFalse(record.events[0]['absolute_onset_validated'])

    def test_invalid_rows_are_rejected_with_source_line(self):
        for column, value in ((0, 1), (1, '00:00.001'), (2, 'nan'), (2, 'inf'), (1, '00:60.001')):
            with self.subTest(column=column, value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'bad.csv'
                def change(i, row):
                    if i == 6:
                        row[column] = value
                    return row
                make_bl(path, change=change)
                with self.assertRaisesRegex(BLInputError, 'source line 10'):
                    load_bl422(path)

    def test_wrong_count_rate_and_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'bad.csv'
            for before, after in [('100个', '101个'), ('1000Hz', '500Hz'), ('(mV)', '(A)')]:
                make_bl(p, encoding='utf-8')
                p.write_text(p.read_text(encoding='utf-8').replace(before, after), encoding='utf-8')
                with self.assertRaises(BLInputError):
                    load_bl422(p)

    def test_time_precision_and_hours(self):
        self.assertEqual(parse_time_us(' 01:02:03.000125 '), 3723000125)
        self.assertEqual(parse_time_us('62:03.000125'), 3723000125)
        for text in ('1', '-1:00', '00:NaN', '01:60:00', '00:00.0000001'):
            with self.assertRaises(BLInputError):
                parse_time_us(text)

    def test_parameters_preserve_source_and_apply_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, params = Path(tmp)/'bl.csv', Path(tmp)/'params.csv'
            make_bl(p)
            with load_bl422(p) as record:
                export_parameter_template(record, params)
                text = params.read_text(encoding='utf-8-sig')
                first_id = record.events[0]['event_id']
                params.write_text(text.replace(first_id+',,', first_id+',0.2,记录'), encoding='utf-8-sig')
                apply_parameters(record, params)
                self.assertEqual(record.events[0]['pulse_width_ms'], .2)
                self.assertEqual(record.events[0]['commanded_current_ma'], -.5)
                self.assertEqual(record.events[0]['raw_event_text'], 'Stimulate: -0.50mA')
                params.write_text('event_id,pulse_width_ms,notes\n'+first_id+',.5,a\nunknown,.5,b\n', encoding='utf-8')
                with self.assertRaises(BLInputError):
                    apply_parameters(record, params)
                self.assertEqual(record.events[0]['pulse_width_ms'], .2)


if __name__ == '__main__':
    unittest.main()
