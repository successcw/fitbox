"""Conservative ingestion of completed, explicitly dated exercise messages."""
import fcntl
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from pull import LOG
SPORTS = r'(篮球|羽毛球|乒乓球|网球|足球|排球|跑步|游泳|骑车|骑行|散步|快走)'
NUMBER = r'(?:\d+(?:\.\d+)?|一个半|一个|一|两|二|三|四|五|六|七|八|九|十|半)'
DURATION = rf'({NUMBER})(?:个)?(小时半|小时|分钟)'


def parse_training(record):
    """Return a log row only for a whole, unambiguous completed exercise statement."""
    if record.get('kind') not in ('voice', 'text') or record.get('update_id') is None:
        return None
    raw = record.get('text') or ''
    text = re.sub(r'\s+', '', raw)
    text = re.sub(r'[,，。.!！]+$', '', text)
    text = re.sub(r'[,，]?(?:请)?(?:帮我)?记录(?:一下|下来)?$', '', text)
    # Full matching intentionally leaves questions, negations, plans and mixed messages pending.
    prefix = r'(昨天|今天)(?:我)?(?:打了|打过了|练了|运动了|进行了|完成了|跑了|游了|骑了|走了)'
    match = re.fullmatch(prefix + DURATION + SPORTS, text)
    if match:
        day_word, number, unit, sport = match.groups()
    else:
        match = re.fullmatch(prefix + SPORTS + DURATION, text)
        if not match:
            return None
        day_word, sport, number, unit = match.groups()
    values = {'一个半': 1.5, '一个': 1, '一': 1, '两': 2, '二': 2, '三': 3,
              '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '半': .5}
    value = values[number] if number in values else float(number)
    minutes = value if unit == '分钟' else (value + (.5 if unit == '小时半' else 0)) * 60
    if not 0 < minutes <= 24 * 60:
        return None
    try:
        day = datetime.fromisoformat(record['ts']).date()
    except (KeyError, ValueError, TypeError):
        return None
    if day_word == '昨天':
        day -= timedelta(days=1)
    return {'date': day.isoformat(), 'time': None, 'source': 'telegram',
            'weight': None, 'pain_hip': None, 'stiff_hip': None,
            'done': [f'{sport}{minutes:g}分钟'], 'food': None, 'note': None,
            'raw': raw, 'tg_update_id': record['update_id']}


def ingest(record, log_path=None):
    """Append exactly once by update_id; return the parsed/existing row, or None."""
    row = parse_training(record)
    if row is None:
        return None
    path = Path(log_path) if log_path is not None else LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        for line in stream:
            if not line.strip():
                continue
            existing = json.loads(line)  # Do not silently skip corrupt history and risk duplicates.
            if str(existing.get('tg_update_id')) == str(row['tg_update_id']):
                return existing
        stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        stream.flush()
    return row
