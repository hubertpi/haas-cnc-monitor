from pathlib import Path
import ast
import yaml
from app.collector import MDC_QUERIES, MDC_DIAGNOSTIC_VARIABLES, CollectionError, factory
import pytest

ROOT=Path(__file__).resolve().parents[1]

def test_mdc_queries_are_exact_documented_reads():
    assert MDC_QUERIES==frozenset(('?Q100','?Q101','?Q102','?Q104','?Q200','?Q201','?Q300','?Q301','?Q303','?Q304','?Q402','?Q403','?Q500','?Q600 3027')) | frozenset('?Q600 '+str(v) for v in (62593,3022,5041,5042,5043,5044,5045,5046))
    assert not any(q.startswith('?E') for q in MDC_QUERIES)


def test_network_collection_only_uses_get_and_allowlisted_mdc_query():
    tree=ast.parse((ROOT/'haas_monitor/app/collector.py').read_text())
    streams=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='stream']
    assert len(streams)==1 and isinstance(streams[0].args[0],ast.Constant) and streams[0].args[0].value=='GET'
    assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ('eval','exec') for n in ast.walk(tree))
    source=(ROOT/'haas_monitor/app/collector.py').read_text()
    assert 'if command not in MDC_QUERIES' in source


def test_monitor_controls_cannot_route_machine_commands():
    source=(ROOT/'haas_monitor/app/mqtt.py').read_text()
    assert source.count('.subscribe(')==2
    assert '"/machine/+/monitor/set/+"' in source
    assert 'settings = {"boost"' in source
    assert not (ROOT/'haas_monitor/native').exists() and not (ROOT/'haas_monitor/worker').exists()


def test_live_mode_requires_explicit_enable_and_addon_has_no_host_privileges():
    with pytest.raises(CollectionError):factory({'mode':'haas'})
    with pytest.raises(CollectionError):factory({'mode':'unknown','hardware_testing_enabled':True})
    config=yaml.safe_load((ROOT/'haas_monitor/config.yaml').read_text())
    assert config['options']['mode']=='simulation' and not config['options']['hardware_testing_enabled']
    assert config['ingress'] and 'ports' not in config
    assert not config.get('privileged') and not config.get('docker_api')
