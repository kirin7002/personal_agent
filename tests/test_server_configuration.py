"""Server configuration never duplicates approved model secrets into Hermes home."""
import importlib.util
import json
from pathlib import Path


def test_server_shared_environment_keeps_secrets_in_one_file(tmp_path,monkeypatch,capsys):
    source=Path(__file__).resolve().parents[1]/'scripts/configure_local_hermes.py'
    spec=importlib.util.spec_from_file_location('configure_private_server_test',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root=tmp_path;home=root/'.runtime/hermes-home'
    defaults=root/'.runtime/hermes-agent/hermes_cli/config_defaults.py'
    defaults.parent.mkdir(parents=True);defaults.write_text('CONFIG={"_config_version":12}')
    for name in ['bailian_limits','vehicle_completion']:
        folder=root/'hermes_plugins'/name;folder.mkdir(parents=True);(folder/'__init__.py').write_text('')
    backend='fixture-backend-credential';internal='fixture-internal-credential'
    (root/'.env').write_text(f'DASHSCOPE_API_KEY={backend}\nHERMES_API_KEY={internal}\nQWEN_AUDIO_API_KEY=fixture-audio-credential\n')
    monkeypatch.setattr(module,'ROOT',root);monkeypatch.setattr(module,'RUNTIME_HOME',home)
    monkeypatch.setattr('sys.argv',['configure','--shared-env'])
    module.main()
    assert (home/'.env').read_text()=='API_SERVER_ENABLED=true\n'
    assert not (home/'.env').stat().st_mode & 0o077
    config=(home/'config.yaml').read_text()
    assert backend not in config and internal not in config
    assert json.loads(config)['providers']['vehicle-bailian']['key_env']=='DASHSCOPE_API_KEY'
    assert backend in (root/'.env').read_text()
    output=capsys.readouterr().out
    assert backend not in output and internal not in output
