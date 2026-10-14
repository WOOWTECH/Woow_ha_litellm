"""Exercise the real proxy route, auth builder and wrapper; no DB/provider/network."""
import asyncio
import httpx
import pytest
from unittest.mock import AsyncMock
from litellm.proxy.auth.resolvers import store
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy import proxy_server as server
MASTER='sk-local-routes-regression-master'
@pytest.fixture
def app(monkeypatch):
 monkeypatch.setattr(server,'master_key',MASTER)
 monkeypatch.setattr(server,'general_settings',{})
 monkeypatch.setattr(server,'prisma_client',object())
 monkeypatch.setattr(server,'user_api_key_cache',UserApiKeyCache())
 # Replace only DB I/O with an absent key, preserving real auth/cache/resolver behavior.
 monkeypatch.setattr(store,'_fetch_key_object_from_db_with_reconnect',AsyncMock(return_value=None))
 monkeypatch.setattr(server,'user_custom_auth',None)
 return server.app
async def get(app,path,token=None):
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.invalid') as c:
  return await c.get(path,headers={'Authorization':'Bearer '+token} if token else {})
@pytest.mark.parametrize('token',[None,'sk-invalid-local-fixture'])
def test_routes_rejects_anonymous_and_invalid_key(app,token):
 r=asyncio.run(get(app,'/routes',token))
 assert r.status_code==401
 assert 'routes' not in r.json()
def test_routes_allows_master(app):
 r=asyncio.run(get(app,'/routes',MASTER))
 assert r.status_code==200 and isinstance(r.json()['routes'],list)
@pytest.mark.parametrize('path',['/health/liveness','/health/liveliness'])
def test_public_health_unchanged(app,path):
 assert asyncio.run(get(app,path)).status_code==200

@pytest.mark.parametrize('path',['/routes?debug=1','/routes/'])
def test_query_and_redirect_do_not_bypass_auth(app,path):
 async def run():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.invalid',follow_redirects=True) as c:return await c.get(path)
 assert asyncio.run(run()).status_code==401
