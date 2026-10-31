"""Single-user deployment boundary, checked before opening data or loading models."""
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit
import os


@dataclass(frozen=True)
class BrowserAccess:
    mode: str
    username: str
    password: str

    def permits_anonymous(self,request):
        if self.mode!='local':return False
        # Forwarding is unsupported in anonymous mode. A public proxy must use
        # public mode and authentication, even when its upstream is loopback.
        if any(k=='forwarded' or k.startswith('x-forwarded-') or k=='x-real-ip' for k in request.headers):return False
        try:
            peer=ip_address(request.client.host)
            if not peer.is_loopback and not (peer.version==6 and peer.ipv4_mapped and peer.ipv4_mapped.is_loopback):return False
            host=request.headers.get('host','')
            if '@' in host:return False
            parsed=urlsplit('//'+host);parsed.port  # Validate bracket and port syntax too.
            hostname=(parsed.hostname or '').lower()
            if hostname=='localhost':return True
            address=ip_address(hostname)
            return address.is_loopback or bool(address.version==6 and address.ipv4_mapped and address.ipv4_mapped.is_loopback)
        except (ValueError,AttributeError):return False


def browser_access():
    mode=os.getenv('TECHDOC_DEPLOYMENT_MODE','public')
    if mode not in ('local','public'):raise RuntimeError('INVALID_DEPLOYMENT_MODE')
    user=os.getenv('TECHDOC_BROWSER_USER','');password=os.getenv('TECHDOC_BROWSER_PASSWORD','')
    if bool(user)!=bool(password):raise RuntimeError('Both browser credentials must be configured')
    if (user and (not user.strip() or ':' in user)) or (password and not password.strip()):
        raise RuntimeError('INVALID_BROWSER_CREDENTIALS')
    if mode=='public' and not password:raise RuntimeError('PUBLIC_AUTH_REQUIRED')
    return BrowserAccess(mode,user,password)
