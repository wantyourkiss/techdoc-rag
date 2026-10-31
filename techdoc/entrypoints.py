"""Explicit HTTPS release probes. No environment proxies, redirects or raw errors."""
import base64,http.client,ipaddress,json,socket,ssl
from datetime import datetime,timezone
from urllib.parse import urlsplit

CONTRACT='three_entry_health_v1'


def _target(url):
    try:
        p=urlsplit(url)
        if (p.scheme!='https' or not p.hostname or p.username is not None or p.password is not None
            or p.path not in ('','/') or p.query or p.fragment):raise ValueError()
        port=443 if p.port is None else p.port
        if not 1<=port<=65535:raise ValueError()
        return p.hostname.encode('idna').decode('ascii'),port
    except (ValueError,TypeError,UnicodeError):raise ValueError('INVALID_HTTPS_ENTRYPOINT') from None


def _request(host,port,address,family,context,credentials,timeout):
    with socket.socket(family,socket.SOCK_STREAM) as raw:
        raw.settimeout(timeout);raw.connect(address)
        with context.wrap_socket(raw,server_hostname=host) as connection:
            authority=('['+host+']') if ':' in host else host
            header=''
            if credentials:
                header='Authorization: Basic '+base64.b64encode((credentials[0]+':'+credentials[1]).encode()).decode()+'\r\n'
            connection.sendall(('GET /health HTTP/1.1\r\nHost: '+authority+':'+str(port)+'\r\n'+header+'Connection: close\r\n\r\n').encode('ascii'))
            response=http.client.HTTPResponse(connection);response.begin()
            body=response.read(4097)
            if len(body)>4096:raise ValueError('HEALTH_BODY_LIMIT')
            return response.status,response.getheader('WWW-Authenticate',''),body


def _probe(name,url,family,ca,credentials,timeout):
    host,port=_target(url);value={'entry':name,'host':host,'port':port,'family':4 if family==socket.AF_INET else 6,'addresses':[],'passed':False}
    try:
        context=ssl.create_default_context(cafile=ca)
        records=socket.getaddrinfo(host,port,family,socket.SOCK_STREAM)
        addresses=list(dict.fromkeys(row[4] for row in records))
        if not 1<=len(addresses)<=8:raise ValueError('ENTRYPOINT_ADDRESS_BUDGET')
        if name!='lan' and any(not ipaddress.ip_address(address[0]).is_global
            or (ipaddress.ip_address(address[0]).version==6 and ipaddress.ip_address(address[0]).ipv4_mapped)
            for address in addresses):
            raise ValueError('PUBLIC_ENTRYPOINT_NOT_GLOBAL')
        for address in addresses:
            row={'address':str(ipaddress.ip_address(address[0])),'tls_verified':False,'passed':False}
            value['addresses'].append(row)
            try:
                status,challenge,_=_request(host,port,address,family,context,None,timeout)
                row.update(anonymous_status=status,tls_verified=True)
                if status!=401 or not challenge.lower().startswith('basic '):raise ValueError('ANONYMOUS_AUTH_REQUIRED')
                status,_,body=_request(host,port,address,family,context,credentials,timeout)
                row['authenticated_status']=status
                if status!=200:raise ValueError('AUTHENTICATED_HEALTH_FAILED')
                # Never persist provider errors, directories or reflected headers.
                if json.loads(body)!={'status':'ready'}:raise ValueError('HEALTH_NOT_MINIMAL_READY')
                row.update(health_status='ready',passed=True)
            except ssl.SSLError:row.update(tls_verified=False,error_code='TLS_CHECK_FAILED')
            except (OSError,TimeoutError,http.client.HTTPException):row['error_code']='ENTRYPOINT_TRANSPORT_FAILED'
            except (ValueError,UnicodeError) as exc:
                allowed={'ANONYMOUS_AUTH_REQUIRED','AUTHENTICATED_HEALTH_FAILED','HEALTH_NOT_MINIMAL_READY','HEALTH_BODY_LIMIT'}
                row['error_code']=str(exc) if str(exc) in allowed else 'INVALID_HEALTH_RESPONSE'
        value['passed']=all(row['passed'] for row in value['addresses'])
    except ssl.SSLError:value['error_code']='TLS_CONFIGURATION_FAILED'
    except (OSError,ValueError,UnicodeError):value['error_code']='ENTRYPOINT_RESOLUTION_OR_CONFIGURATION_FAILED'
    return value


def verify_entrypoints(*,lan_url,public_url,lan_ca,credentials,timeout=8):
    if (not isinstance(credentials,(tuple,list)) or len(credentials)!=2
        or any(not isinstance(v,str) or not v for v in credentials)):raise ValueError('BROWSER_CREDENTIALS_REQUIRED')
    if type(timeout) not in (int,float) or not 1<=timeout<=30:raise ValueError('INVALID_ENTRYPOINT_TIMEOUT')
    _target(lan_url);_target(public_url)
    lan_host,_=_target(lan_url)
    try:family=socket.AF_INET6 if ipaddress.ip_address(lan_host).version==6 else socket.AF_INET
    except ValueError:family=socket.AF_INET
    checks=[_probe('lan',lan_url,family,lan_ca,credentials,timeout),
        _probe('ipv4',public_url,socket.AF_INET,None,credentials,timeout),
        _probe('ipv6',public_url,socket.AF_INET6,None,credentials,timeout)]
    return {'contract':CONTRACT,'checked_at':datetime.now(timezone.utc).isoformat(),
        'passed':all(row['passed'] for row in checks),'checks':checks,'model_calls':0}


def require_verified(report):
    """A top-level success flag alone never authorizes publishing a release."""
    if not isinstance(report,dict) or report.get('contract')!=CONTRACT or report.get('passed') is not True:raise ValueError('THREE_ENTRY_HEALTH_FAILED')
    checks=report.get('checks',[])
    if (not isinstance(checks,list) or len(checks)!=3 or any(not isinstance(row,dict) for row in checks)
        or {row.get('entry') for row in checks}!={'lan','ipv4','ipv6'}):raise ValueError('THREE_ENTRY_HEALTH_FAILED')
    for row in checks:
        addresses=row.get('addresses',[])
        family=row.get('family');expected={'ipv4':4,'ipv6':6}.get(row['entry'],family)
        if (family not in (4,6) or family!=expected or row.get('passed') is not True
            or not isinstance(addresses,list) or not 1<=len(addresses)<=8):raise ValueError('THREE_ENTRY_HEALTH_FAILED')
        for address in addresses:
            if not isinstance(address,dict):raise ValueError('THREE_ENTRY_HEALTH_FAILED')
            try:ip=ipaddress.ip_address(address['address'])
            except (ValueError,KeyError):raise ValueError('THREE_ENTRY_HEALTH_FAILED') from None
            if (ip.version!=family or (row['entry']!='lan' and (not ip.is_global or (ip.version==6 and ip.ipv4_mapped)))
                or address.get('passed') is not True or address.get('tls_verified') is not True
                or address.get('anonymous_status')!=401 or address.get('authenticated_status')!=200
                or address.get('health_status')!='ready'):raise ValueError('THREE_ENTRY_HEALTH_FAILED')
