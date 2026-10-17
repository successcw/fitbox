"""指标边界与个人基线灯号，不访问网络。"""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from health_daily import cpap_indicators

class IndicatorTests(unittest.TestCase):
    def test_ahi_and_leak_have_distinct_boundaries(self):
        x=cpap_indicators({'ahi':5,'leak95':24})
        self.assertEqual(x[0]['level'],'warn')
        self.assertEqual(x[2]['level'],'good')
        self.assertEqual(cpap_indicators({'ahi':4.9,'leak95':24.1})[0]['level'],'good')
        self.assertEqual(cpap_indicators({'ahi':4.9,'leak95':24.1})[2]['level'],'warn')

    def test_rr_compares_prior_personal_range(self):
        cp={'rr50':17.4,'rr_low':16.6,'rr_high':17.4,'rr_baseline_n':7}
        self.assertEqual(cpap_indicators(cp)[1]['level'],'good')
        cp['rr50']=18
        self.assertEqual(cpap_indicators(cp)[1]['level'],'warn')
        cp['rr_baseline_n']=2
        self.assertEqual(cpap_indicators(cp)[1]['level'],'unknown')

    def test_missing_or_invalid_is_not_green(self):
        for cp in [None,{}, {'ahi':float('nan'),'leak95':-1,'rr50':0}]:
            self.assertTrue(all(m['level']=='unknown' for m in cpap_indicators(cp)))

if __name__=='__main__':unittest.main()
