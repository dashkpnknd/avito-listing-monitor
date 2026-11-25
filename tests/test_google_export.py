from unittest import TestCase
from xml.etree import ElementTree as ET

from avito_monitor.google_export import X, exported_cell
from avito_monitor.model import id_state, price_state


class GoogleExportTests(TestCase):
    def test_empty_shared_string_is_blank_but_empty_formula_is_not(self):
        ns = X[1:-1]
        blank = ET.fromstring(f'<c xmlns="{ns}" t="s"><v>0</v></c>')
        formula = ET.fromstring(f'<c xmlns="{ns}" t="str"><f>IF(TRUE,"","x")</f><v/></c>')
        self.assertEqual("EMPTY", id_state(exported_cell(blank, [""])))
        self.assertEqual("INVALID", price_state(exported_cell(formula, [""]))[0])
