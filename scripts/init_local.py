#!/usr/bin/env python3
"""Create only missing local configuration and data folders; never deploy services."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
cfg = root / 'config.local.env'
if not cfg.exists():
    shutil.copyfile(root / 'config.example.env', cfg)
    cfg.chmod(0o600)
for directory in ('数据/健康/raw', '数据/fitbox', '康复日志', '呼吸机'):
    (root / 'runtime' / directory).mkdir(parents=True, exist_ok=True)
(root / 'runtime/康复日志/log.jsonl').touch(exist_ok=True)
print('已初始化本项目 runtime/，未连接任何设备。')
print('编辑本地配置：', cfg)
print('常驻服务请按 docs/deployment.md 配置 ~/.config/fitbox/config.env。')
