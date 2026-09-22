"""Read-only consistent database snapshot for the audit; no production writes."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, select
from app.config import get_settings
from app.models import Base

ROOT = Path(__file__).resolve().parent
TABLES = ['candles','signals','trades','option_contracts','option_candles',
          'market_regimes','vix_daily','paper_positions','paper_decisions']
source = create_engine(get_settings().database_url)
destination = ROOT / 'snapshot.sqlite'
if destination.exists():
    raise SystemExit('Snapshot already exists; refusing to overwrite audit evidence.')
target = create_engine('sqlite:///' + str(destination))
Base.metadata.create_all(target)
counts = {}
with source.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
    with conn.begin():
        conn.exec_driver_sql('SET TRANSACTION READ ONLY')
        with target.begin() as out:
            for name in TABLES:
                table = Base.metadata.tables[name]
                rows = [dict(r) for r in conn.execute(select(table).order_by(table.c.id)).mappings()]
                for offset in range(0, len(rows), 1000):
                    out.execute(table.insert(), rows[offset:offset+1000])
                counts[name] = len(rows)
manifest = {'captured_at_utc':datetime.now(timezone.utc).isoformat(), 'counts':counts,
            'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'scope':'consistent read-only snapshot; local research copy'}
(ROOT/'manifest.json').write_text(json.dumps(manifest, indent=2))
print(json.dumps(manifest,indent=2))
