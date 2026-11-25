"""Circuit-map checks, including real reverse-supply and isolation counterexamples."""
import unittest
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from scripts.build_system_breadboard import make_layout, verify


class SystemBreadboardTests(unittest.TestCase):
    def test_delivered_diagrams_match_profile_wire_tables(self):
        docs = Path(__file__).resolve().parents[2] / 'Docs'
        for profile, stem in [('right', 'SYSTEM_MODULE_BREADBOARD'),
                              ('original', 'SYSTEM_MODULE_BREADBOARD_PA0_PA1')]:
            with self.subTest(profile=profile):
                data = make_layout(profile)
                page = (docs / (stem + '.html')).read_text(encoding='utf-8')
                wire_json = page.split('const wires=', 1)[1].split(';let selected=', 1)[0]
                self.assertEqual(json.loads(wire_json), data['wires'])
                self.assertIn('f13=' + data['mcu']['start'] + '/START', page)
                self.assertIn('本图配套固件目标：' + data['firmware_target'], page)
                svg = ET.parse(docs / (stem + '.svg')).getroot()
                ids = {el.attrib.get('id') for el in svg.iter()}
                self.assertTrue({w['id'] for w in data['wires']} <= ids)
                self.assertTrue((docs / (stem + '.png')).is_file())

    def test_both_profiles_and_exactly_three_mcu_endpoint_changes(self):
        old, new = make_layout('original'), make_layout('right')
        self.assertEqual(verify(old)['status'], 'passed')
        self.assertEqual(verify(new)['status'], 'passed')
        changes = []
        for a, b in zip(old['wires'], new['wires']):
            self.assertEqual({k: v for k, v in a.items() if k != 'a'},
                             {k: v for k, v in b.items() if k != 'a'})
            if a['a'] != b['a']:
                changes.append((b['stage'], b['name'], a['a'], b['a'], b['b']))
        self.assertCountEqual(changes, [('common', 'START', 'PB8', 'PB4', 'f13'),
                                       ('A', 'DRDY', 'PA0', 'PB8', 'j1'),
                                       ('B', 'DRDY', 'PA1', 'PB2', 'j15')])

    def test_old_start_endpoint_in_right_profile_is_rejected(self):
        data = make_layout('right')
        next(w for w in data['wires'] if w['stage'] == 'common' and w['name'] == 'START')['a'] = 'PB8'
        with self.assertRaises(AssertionError):
            verify(data)

    def test_requested_module_positions_and_valid_wiring(self):
        data=make_layout()
        self.assertEqual(data['pins']['A']['3V3'],'c9')
        self.assertEqual(data['pins']['A']['GND'],'c8')
        self.assertEqual(data['pins']['B']['3V3'],'c23')
        self.assertEqual(data['pins']['B']['GND'],'c22')
        self.assertEqual(data['pins']['A']['DRDY'],'g1')
        self.assertEqual(data['pins']['B']['DRDY'],'g15')
        self.assertEqual(verify(data)['status'],'passed')

    def test_reversed_b_power_wires_are_rejected(self):
        data=make_layout()
        for w in data['wires']:
            if w['stage']=='B' and w['name']=='3V3':w['b']='a22'
            elif w['stage']=='B' and w['name']=='GND':w['b']='a23'
        with self.assertRaises(AssertionError):verify(data)

    def test_shared_sclk_is_rejected(self):
        data=make_layout()
        next(w for w in data['wires'] if w['stage']=='B' and w['name']=='SCLK')['a']='PA5'
        with self.assertRaises(AssertionError):verify(data)

    def test_b_signal_connected_in_a_only_mode_is_rejected(self):
        data=make_layout()
        next(w for w in data['wires'] if w['stage']=='B' and w['name']=='CS')['stage']='common'
        with self.assertRaises(AssertionError):verify(data)


if __name__=='__main__':unittest.main()
