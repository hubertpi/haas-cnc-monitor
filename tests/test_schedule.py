from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from app.schedule import ScheduleInput,ScheduleStore,defaults,day_shifts
from app.utilization import Utilization
from app.main import create_app


def stamp(text,zone='America/Chicago'):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp()

def config(**changes):
    return ScheduleInput.model_validate({**defaults().model_dump(),**changes})

@pytest.mark.parametrize('changes',[
    {'timezone':'Invalid/Place'}, {'weekdays':[]}, {'weekdays':[1,1]}, {'weekdays':[7]},
    {'shifts':[]}, {'shifts':[{'name':'Bad','start':'06:00','end':'06:00'}]},
    {'shifts':[{'name':'Bad','start':'06:00','end':'16:00','breaks':[{'start':'17:00','minutes':10}]}]},
    {'shifts':[{'name':'Bad','start':'06:00','end':'16:00','breaks':[{'start':'08:30','minutes':20},{'start':'08:40','minutes':20}]}]},
    {'shifts':[{'name':'A','start':'06:00','end':'16:00'},{'name':'B','start':'15:00','end':'02:00'}]},
    {'weekdays':[0,6],'shifts':[{'name':'A','start':'23:00','end':'07:00'},{'name':'B','start':'06:00','end':'10:00'}]},
])
def test_schedule_rejects_invalid_and_overlapping(changes):
    with pytest.raises(ValidationError):config(**changes)

def test_three_shifts_overnight_break_and_dst():
    value=config(weekdays=[6],shifts=[
        {'name':'A','start':'00:00','end':'08:00','breaks':[{'start':'03:00','minutes':30}]},
        {'name':'B','start':'08:00','end':'16:00'},
        {'name':'C','start':'16:00','end':'00:00'}])
    rows=day_shifts(datetime(2026,11,1).date(),value)
    assert len(rows)==3
    assert rows[0][2]-rows[0][1]==9*3600 # Fall-back Sunday actual elapsed time
    assert rows[2][2]-rows[2][1]==8*3600

def test_edits_preserve_totals_and_prior_planned_time(tmp_path):
    schedule=ScheduleStore(tmp_path/'schedule.json')
    stats=Utilization(tmp_path/'stats.db',schedule=schedule)
    start=stamp('2026-10-12T06:00')
    signals={k:{'value':v} for k,v in {'mode':'AUTOMATIC','execution':'ACTIVE','feed_rate':4}.items()}
    stats.observe('m',signals,start);stats.observe('m',signals,start+1)
    edit=stamp('2026-10-12T07:00')
    value=config(revision=1,shifts=[{'name':'Only shift','start':'08:00','end':'17:00'}])
    schedule.save(value,edit)
    stats.observe('m',signals,stamp('2026-10-12T08:00'));stats.observe('m',signals,stamp('2026-10-12T08:00:01'))
    result=stats.summary('m',stamp('2026-10-12T08:00:01'))
    assert result['month']['production_seconds']==2
    # The original 06:00–07:00 planned hour remains; the new gap doesn't count.
    assert sum(b-a for _,a,b in stats.segments(start,stamp('2026-10-12T08:00:01')))==3601
    assert 'shift_2' not in result
    stats.close()
    restored=ScheduleStore(tmp_path/'schedule.json')
    assert restored.revision==2 and len(restored.current.shifts)==1
    with pytest.raises(ValueError):restored.save(value,edit+1)

def test_schedule_api_csrf_restart_and_revision(tmp_path):
    with TestClient(create_app(tmp_path,{'mode':'simulation'},development=True)) as client:
        state=client.get('/api/status').json()
        assert len(state['schedule']['shifts'])==2
        value={**state['schedule'],'shifts':[{'name':'Day','start':'07:00','end':'15:00','breaks':[]}]}
        value.pop('effective')
        assert client.put('/api/schedule',json=value).status_code==403
        headers={'x-monitor-csrf':state['csrf']}
        response=client.put('/api/schedule',json=value,headers=headers)
        assert response.status_code==200,response.text
        assert response.json()['revision']==2
        assert client.put('/api/schedule',json=value,headers=headers).status_code==409
    with TestClient(create_app(tmp_path,{'mode':'simulation'},development=True)) as client:
        assert client.get('/api/status').json()['schedule']['shifts'][0]['start']=='07:00'


def test_timezone_change_keeps_current_calendar_totals(tmp_path):
    schedule=ScheduleStore(tmp_path/'schedule.json')
    stats=Utilization(tmp_path/'stats.db',schedule=schedule)
    signals={k:{'value':v} for k,v in {'mode':'AUTOMATIC','execution':'ACTIVE','feed_rate':4}.items()}
    a=stamp('2026-10-12T06:00');edit=stamp('2026-10-12T07:00')
    stats.observe('m',signals,a);stats.observe('m',signals,a+1)
    schedule.save(config(revision=1,timezone='UTC'),edit)
    stats.observe('m',signals,edit+1);stats.observe('m',signals,edit+2)
    assert stats.summary('m',edit+2)['week']['production_seconds']==2
    assert stats.summary('m',edit+2)['month']['production_seconds']==2
    stats.close()
