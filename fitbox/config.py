"""公开版配置：环境变量优先，其次 ~/.config/fitbox/config.env。"""
import os
import shlex
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
_default = str(PROJECT / 'config.local.env') if (PROJECT / 'config.local.env').is_file() else '~/.config/fitbox/config.env'
CONFIG_FILE = Path(os.environ.get('FITBOX_CONFIG', _default)).expanduser()
if CONFIG_FILE.is_file():
    for raw in CONFIG_FILE.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith('#') or '=' not in raw:
            continue
        key, value = raw.split('=', 1)
        parts = shlex.split(value, comments=True)
        os.environ.setdefault(key.strip(), ' '.join(parts))

PROJECT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get('FITBOX_DATA_ROOT', str(PROJECT / 'runtime'))).expanduser().resolve()
HOSTS = [x.strip() for x in os.environ.get('FITBOX_HOSTS', 'fitbox.local').split(',') if x.strip()]
SSH_USER = os.environ.get('FITBOX_SSH_USER', '')
TIMEZONE = os.environ.get('FITBOX_TIMEZONE', 'Asia/Shanghai')
WEIGHT_GOAL = float(os.environ['FITBOX_WEIGHT_GOAL_KG']) if os.environ.get('FITBOX_WEIGHT_GOAL_KG') else None


def ssh_target(host):
    return f'{SSH_USER}@{host}' if SSH_USER else host
