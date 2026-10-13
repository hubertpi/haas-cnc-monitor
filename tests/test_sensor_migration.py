import json
from app.mqtt import Publisher, key
from app.utilization import Utilization


def test_temperature_capability_migrates_and_survives_unavailable_restart(tmp_path):
    pub = Publisher(tmp_path)
    machine = {'id': 'lathe', 'name': 'Lathe', 'interval': 1}
    signals = {name: {'value': value, 'unit': '°C', 'source': 'cnc_diagnoss', 'group': 'slow'}
               for name, value in [('motor_temperature', 55), ('encoder_temperature', 0)]}
    pub.discovery(machine, signals, {})
    for name, signal in signals.items():
        pub.state('lathe', name, signal, 'now', False)
    # Model the previous version's journal without capability markers.
    del pub.journal['valid_temperatures']
    pub.needs_flush = True
    pub.flush()
    restored = Publisher(tmp_path)
    assert restored.temperature_validated('lathe', 'motor_temperature')
    assert not restored.temperature_validated('lathe', 'encoder_temperature')
    restored.reconcile({'lathe'})
    unavailable = {**signals['motor_temperature'], 'value': None, 'available': False}
    restored.discovery(machine, {'motor_temperature': unavailable}, {})
    restored.state('lathe', 'motor_temperature', unavailable, 'later', False)
    config = json.loads(restored.journal['topics'][restored.discovery_topic('lathe')])
    component = config['components'][key('motor_temperature')]
    assert component['unique_id'] == 'lathe_' + key('motor_temperature')
    assert 'value_json.available' in component['availability'][-1]['value_template']
    assert config['components'][key('encoder_temperature')] == {'platform': 'sensor'}
    state = json.loads(restored.journal['topics'][component['state_topic']])
    assert state['value'] is None and state['available'] is False
    again = Publisher(tmp_path)
    assert again.temperature_validated('lathe', 'motor_temperature')
    again.remove('lathe')
    assert not Publisher(tmp_path).temperature_validated('lathe', 'motor_temperature')


def test_oee_names_and_helper_entity_cleanup_preserve_ids(tmp_path):
    stats = Utilization(tmp_path / 'stats.db')
    current = stats.signals('lathe')
    assert {s['label'] for s in current.values()} == {
        'OEE - 24hr', 'OEE - Week', 'OEE - Month', 'OEE - 1st Shift', 'OEE - 2nd Shift'}
    assert all(s['unit'] == '%' for s in current.values())
    pub = Publisher(tmp_path)
    machine = {'id': 'lathe', 'name': 'Lathe', 'interval': 1}
    old = dict(current)
    for suffix in ('coverage', 'production_seconds', 'planned_seconds', 'unknown_seconds'):
        old['production_shift_1_' + suffix] = {'value': 0, 'source': 'monitor.accounting'}
    pub.discovery(machine, old, {})
    for name, signal in old.items():
        pub.state('lathe', name, signal, 'now', False)
    pub.discovery(machine, current, {})
    config = json.loads(pub.journal['topics'][pub.discovery_topic('lathe')])
    for name in old.keys() - current.keys():
        assert config['components'][key(name)] == {'platform': 'sensor'}
        assert pub.journal['topics'][pub.machine_root('lathe') + '/signal/' + key(name)] == ''
    for name, signal in current.items():
        assert config['components'][key(name)]['unique_id'] == 'lathe_' + key(name)
        assert config['components'][key(name)]['name'] == signal['label']
    stats.close()
