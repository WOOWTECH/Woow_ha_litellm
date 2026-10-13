"""Review regressions: collection redirects and colon IDs keep real auth/ACL."""
import asyncio
from urllib.parse import quote
import httpx
import litellm
import pytest
from fastapi import Request
from litellm.proxy import proxy_server as server
from litellm.proxy.auth.auth_utils import get_model_from_request
from .test_proxy_contract import proxy, seed_virtual, get, MASTER, VIRTUAL, CANARY

@pytest.mark.parametrize('prefix',['/models/','/v1/models/'])
@pytest.mark.parametrize('identity',['admin','virtual','blocked','missing'])
def test_collection_trailing_slash_redirect_keeps_auth(proxy,monkeypatch,prefix,identity):
    if identity in ('virtual','blocked'):
        seed_virtual(monkeypatch,['gpt-4o-mini'],blocked=identity=='blocked')
    token=MASTER if identity=='admin' else VIRTUAL if identity!='missing' else None
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy),base_url='http://offline.invalid',follow_redirects=True) as client:
            return await client.get(prefix+'?healthy_only=false',headers={'Authorization':'Bearer '+token} if token else {})
    result=asyncio.run(run())
    assert len(result.history)==1 and result.history[0].status_code==307
    assert result.history[0].headers['location'].endswith(prefix[:-1]+'?healthy_only=false')
    assert result.status_code in ((401,403) if identity=='blocked' else (401,) if identity=='missing' else (200,))
    if result.status_code==200: assert result.json()['object']=='list'
    assert CANARY not in result.text and MASTER not in result.text and VIRTUAL not in result.text

@pytest.fixture
def colon_proxy(proxy,monkeypatch):
    router=litellm.Router(model_list=[{'model_name':name,'litellm_params':{'model':'openai/gpt-4o-mini','api_key':CANARY}}
        for name in ['plain:v1','org/tag:v1','org/tag:generateContent','plain']],num_retries=0)
    monkeypatch.setattr(server,'llm_router',router)
    monkeypatch.setattr(server,'llm_model_list',router.model_list)
    return proxy

@pytest.mark.parametrize('name',['plain:v1','org/tag:v1','org/tag:generateContent'])
@pytest.mark.parametrize('prefix',['/models/','/v1/models/'])
@pytest.mark.parametrize('identity',['permitted','forbidden','blocked','missing'])
def test_colon_model_id_auth_and_permissions(colon_proxy,monkeypatch,name,prefix,identity):
    if identity!='missing':
        seed_virtual(monkeypatch,[name] if identity!='forbidden' else ['plain'],blocked=identity=='blocked')
    result=asyncio.run(get(colon_proxy,prefix+quote(name,safe=''),VIRTUAL if identity!='missing' else None))
    assert result.status_code in ((401,403) if identity=='blocked' else ({'permitted':200,'forbidden':404,'missing':401}[identity],))
    if identity=='permitted': assert result.json()['id']==name

@pytest.mark.parametrize('permitted',[False,True])
def test_colon_alias_never_grants_target_permission(colon_proxy,monkeypatch,permitted):
    seed_virtual(monkeypatch,['org/tag:v1'] if permitted else ['plain'],aliases={'friendly:alias':'org/tag:v1'})
    result=asyncio.run(get(colon_proxy,'/models/friendly%3Aalias',VIRTUAL))
    assert result.status_code==(200 if permitted else 404)
    if permitted: assert result.json()['id']=='friendly:alias'

@pytest.mark.parametrize('action',['generateContent','streamGenerateContent','countTokens'])
def test_google_post_model_acl_extraction_preserved(action):
    route='/models/org/model:'+action
    req=Request({'type':'http','method':'POST','path':route,'headers':[],'path_params':{'model_name':'org/model'}})
    assert get_model_from_request({},route,request=req)=='org/model'
