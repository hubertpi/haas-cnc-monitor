from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sqlite3
import pytest
from app.utilization import Utilization, planned, shifts

ZONE = ZoneInfo('America/Chicago')
def stamp(text): return datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp()
def readings(mode='AUTOMATIC',execution='ACTIVE',feed=4):
    return {k:{'value':v} for k,v in {'mode':mode,'execution':execution,'feed_rate':feed}.items()}

def test_schedule_breaks_overnight_and_friday():
    start,end = stamp('2026-10-12T06:00'),stamp('2026-10-13T02:00')
    assert planned(start,end,ZONE)==1100*60
    assert planned(stamp('2026-10-12T16:00'),end,ZONE)==550*60
    assert planned(stamp('2026-10-16T16:00'),stamp('2026-10-17T02:00'),ZONE)==550*60
    assert planned(stamp('2026-10-17T06:00'),stamp('2026-10-18T02:00'),ZONE)==0
    for a,b in [('08:30','08:40'),('11:00','11:30'),('13:30','13:40'),('18:30','18:40'),('21:00','21:30'),('23:30','23:40')]:
        assert planned(stamp('2026-10-12T'+a),stamp('2026-10-12T'+b),ZONE)==0

def test_confirmed_production_excludes_mdi_idle_and_gaps(tmp_path):
    stats=Utilization(tmp_path/'stats.db')
    start=stamp('2026-10-12T06:00')
    stats.observe('m',readings(),start)
    stats.observe('m',readings(),start+1)
    stats.observe('m',readings(mode='MANUAL_DATA_INPUT'),start+2)
    stats.observe('m',readings(mode='MANUAL_DATA_INPUT'),start+3)
    stats.observe('m',readings(feed=0),start+4)
    stats.observe('m',readings(),start+100)
    values=stats.summary('m',start+100)['shift_1']
    assert values['production_seconds']==1
    assert values['planned_seconds']==100
    assert values['unknown_seconds']==96
    assert values['utilization']==1
    stats.close()
    restored=Utilization(tmp_path/'stats.db')
    restored.observe('m',readings(),start+101)
    assert restored.summary('m',start+101)['shift_1']['production_seconds']==1
    restored.close()

def test_overnight_period_and_month_anchor(tmp_path):
    stats=Utilization(tmp_path/'stats.db')
    start=stamp('2026-10-12T23:59:59')
    stats.observe('m',readings(),start)
    stats.observe('m',readings(),start+2)
    result=stats.summary('m',start+2)
    assert result['shift_2']['production_seconds']==2
    assert result['shift_2']['start']==datetime.fromtimestamp(stamp('2026-10-12T16:00'),ZoneInfo('UTC')).isoformat()
    a=stamp('2026-09-30T23:59:59')
    stats.gap('m');stats.observe('m',readings(),a);stats.observe('m',readings(),a+2)
    assert stats.boundaries(a+2)['month']==stamp('2026-09-01T00:00')
    stats.close()

def test_retention_keeps_current_aggregate_only(tmp_path):
    stats=Utilization(tmp_path/'stats.db')
    a=stamp('2026-10-01T06:00')
    stats.observe('m',readings(),a);stats.observe('m',readings(),a+1)
    stats.maintain(a+8*86400)
    assert stats.db.execute('SELECT COUNT(*) FROM minutes').fetchone()[0]==0
    assert stats.summary('m',a+8*86400)['month']['production_seconds']==1
    stats.remove('m')
    assert stats.db.execute('SELECT COUNT(*) FROM periods').fetchone()[0]==0
    stats.close()


def test_missing_actual_feed_and_unknown_mode_leave_oee_unavailable(tmp_path):
    stats=Utilization(tmp_path/'stats.db')
    start=stamp('2026-10-12T06:00')
    missing=readings();missing.pop('feed_rate')
    stats.observe('m',missing,start);stats.observe('m',missing,start+1)
    assert stats.signals('m',start+2)['production_shift_1_utilization']['available'] is False
    stats.observe('m',readings(mode='UNKNOWN'),start+2);stats.observe('m',readings(mode='UNKNOWN'),start+3)
    assert stats.summary('m',start+3)['shift_1']['unknown_seconds']==3
    stats.observe('m',readings(),start+4);stats.observe('m',readings(),start+5)
    assert stats.signals('m',start+5)['production_shift_1_utilization']['available'] is True
    stats.close()
