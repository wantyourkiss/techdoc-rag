"""Tests use an explicit anonymous loopback deployment; production defaults stay closed."""
import os
from starlette.testclient import TestClient

# Set before test modules import the module-level ASGI app. Tests of public/default
# startup explicitly remove/override this environment variable.
os.environ['TECHDOC_DEPLOYMENT_MODE']='local'
_original_init=TestClient.__init__

def _loopback_init(self,*args,**kwargs):
    kwargs.setdefault('base_url','http://127.0.0.1')
    kwargs.setdefault('client',('127.0.0.1',50000))
    _original_init(self,*args,**kwargs)

TestClient.__init__=_loopback_init
