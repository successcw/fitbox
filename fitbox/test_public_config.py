"""公开版不能隐式访问原运行项目，也不能在演示时发送消息。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PublicConfigTests(unittest.TestCase):
    def probe(self, settings):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.env'
            path.write_text(settings)
            env = {k:v for k,v in os.environ.items() if not k.startswith(('FITBOX_', 'TELEGRAM_'))}
            env.update(FITBOX_CONFIG=str(path), PYTHONPATH=str(ROOT/'fitbox'))
            script = "import config,body_metrics,json; print(json.dumps({'root':str(config.DATA_ROOT),'target':config.ssh_target('example.local'),'body':body_metrics.metrics(65,500)}))"
            return json.loads(subprocess.check_output([sys.executable,'-c',script],env=env,text=True))

    def test_default_data_is_isolated_and_profile_is_not_invented(self):
        data = self.probe('')
        self.assertEqual(data['root'],str(ROOT/'runtime'))
        self.assertIsNone(data['body'])

    def test_quoted_config_and_explicit_profile(self):
        data = self.probe("FITBOX_SSH_USER='example'\nFITBOX_HEIGHT_CM='170'\nFITBOX_AGE='35'\nFITBOX_SEX='female'\n")
        self.assertEqual(data['target'],'example@example.local')
        self.assertIsInstance(data['body']['bmi'],float)
