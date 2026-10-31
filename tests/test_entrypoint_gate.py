import copy,json,socket,ssl
from unittest.mock import Mock
import pytest
from techdoc import entrypoints as gate
from techdoc.release_state import publish_verified_release


def setup(monkeypatch,broken=None):
    def resolve(host,port,family,kind):
        address='2606:4700:4700::1111' if family==socket.AF_INET6 else ('192.0.2.1' if host=='lan.test' else '1.1.1.1')
        return [(family,socket.SOCK_STREAM,6,'',(address,port))]
    def request(host,port,address,family,context,credentials,timeout):
        assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED
        if broken=='tls' and host=='public.test' and family==socket.AF_INET:raise ssl.SSLError('PRIVATE_RAW_ERROR')
        if credentials is None:return (200 if broken=='anonymous' else 401),'Basic realm="TechDoc RAG"',b'PRIVATE_ANONYMOUS_BODY'
        if broken=='body':return 200,'',b'{"status":"ready","path":"PRIVATE_SERVER_PATH"}'
        if broken=='redirect':return 302,'',b'PRIVATE_REDIRECT_BODY'
        return 200,'',b'{"status":"ready"}'
    monkeypatch.setattr(gate.socket,'getaddrinfo',resolve);monkeypatch.setattr(gate,'_request',request)
    return gate.verify_entrypoints(lan_url='https://lan.test:8765',public_url='https://public.test:18443',
        lan_ca=None,credentials=('PRIVATE_USER','PRIVATE_PASSWORD'))


def test_all_three_families_required_without_insecure_tls_or_secret_reports(monkeypatch):
    report=setup(monkeypatch);gate.require_verified(report)
    assert [r['family'] for r in report['checks']]==[4,4,6]
    assert 'PRIVATE_' not in json.dumps(report) and report['model_calls']==0


@pytest.mark.parametrize('broken',['tls','anonymous','body','redirect'])
def test_any_failed_entry_blocks_success_and_does_not_echo_raw_body(monkeypatch,broken):
    report=setup(monkeypatch,broken);assert not report['passed']
    assert 'PRIVATE_' not in json.dumps(report)
    with pytest.raises(ValueError,match='THREE_ENTRY_HEALTH_FAILED'):gate.require_verified(report)


def test_secondary_dns_address_cannot_hide_behind_one_healthy_address(monkeypatch):
    report=setup(monkeypatch)
    resolve=gate.socket.getaddrinfo
    def two(host,port,family,kind):
        rows=resolve(host,port,family,kind)
        if host=='public.test' and family==socket.AF_INET:rows.append((family,kind,6,'',('1.0.0.1',port)))
        return rows
    request=gate._request
    def failed(*args):
        if args[2][0]=='1.0.0.1':raise OSError('PRIVATE_NETWORK_ERROR')
        return request(*args)
    monkeypatch.setattr(gate.socket,'getaddrinfo',two);monkeypatch.setattr(gate,'_request',failed)
    report=gate.verify_entrypoints(lan_url='https://lan.test',public_url='https://public.test',lan_ca=None,credentials=('u','p'))
    assert not report['passed'] and len(report['checks'][1]['addresses'])==2


@pytest.mark.parametrize('url',['http://public.test','https://user:PRIVATE_PASSWORD@public.test','https://public.test/path','https://public.test/?key=PRIVATE_SECRET','https://public.test:0'])
def test_invalid_targets_rejected_before_credentials_are_sent(monkeypatch,url):
    request=Mock();monkeypatch.setattr(gate,'_request',request)
    with pytest.raises(ValueError,match='INVALID_HTTPS_ENTRYPOINT'):
        gate.verify_entrypoints(lan_url='https://lan.test',public_url=url,lan_ca=None,credentials=('u','p'))
    assert not request.called


def test_public_entry_cannot_be_substituted_with_loopback_or_wrong_family(monkeypatch):
    report=setup(monkeypatch);report['checks'][1]['family']=6
    with pytest.raises(ValueError):gate.require_verified(report)
    report=setup(monkeypatch);report['checks'][1]['addresses'][0]['address']='127.0.0.1'
    with pytest.raises(ValueError):gate.require_verified(report)
    report=setup(monkeypatch);report['checks'][2]['addresses'][0]['address']='::ffff:1.1.1.1'
    with pytest.raises(ValueError):gate.require_verified(report)


def test_failed_gate_and_forged_success_flag_cannot_publish(tmp_path,monkeypatch):
    report=setup(monkeypatch,'tls');pointer=tmp_path/'current.json';result=tmp_path/'result.json'
    pointer.write_bytes(b'old-pointer')
    record={'deployed':True,'entrypoints_verified':True,'entrypoint_health':report}
    with pytest.raises(ValueError):publish_verified_release(pointer,result,{'release':'new'},record)
    report['passed']=True
    with pytest.raises(ValueError):publish_verified_release(pointer,result,{'release':'new'},record)
    assert pointer.read_bytes()==b'old-pointer' and not result.exists()


def test_result_write_failure_restores_both_previous_records(tmp_path,monkeypatch):
    report=setup(monkeypatch);pointer=tmp_path/'current.json';result=tmp_path/'result.json'
    pointer.write_bytes(b'old-pointer');result.write_bytes(b'old-result')
    import techdoc.release_state as state
    original=state._atomic;failed=False
    def fail_once(path,raw):
        nonlocal failed
        if path==result and not failed:failed=True;raise OSError('PRIVATE_DISK_ERROR')
        original(path,raw)
    monkeypatch.setattr(state,'_atomic',fail_once)
    with pytest.raises(OSError):publish_verified_release(pointer,result,{'release':'new'},
        {'deployed':True,'entrypoints_verified':True,'entrypoint_health':report})
    assert pointer.read_bytes()==b'old-pointer' and result.read_bytes()==b'old-result'
    assert not list(tmp_path.glob('.release-*'))
