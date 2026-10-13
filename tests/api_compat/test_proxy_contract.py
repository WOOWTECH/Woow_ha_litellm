"""Actual FastAPI routes/auth and proxy processing; database/provider remain offline."""
import asyncio
import datetime
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from fastapi import Request, Response
import litellm
from litellm.proxy import proxy_server as server
from litellm.proxy._types import UserAPIKeyAuth, LitellmUserRoles, LiteLLM_UserTable
from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing as Processing
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.types.utils import ModelResponse

MASTER='sk-offline-master-fixture'
VIRTUAL='sk-offline-virtual-fixture'
CANARY='offline-provider-secret-sentinel'

@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setattr(server,'master_key',MASTER)
    monkeypatch.setattr(server,'general_settings',{'custom_auth_run_common_checks':True})
    monkeypatch.setattr(server,'prisma_client',None)
    monkeypatch.setattr(server,'user_custom_auth',None)
    monkeypatch.setattr(server,'user_model',None)
    from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
    cache=UserApiKeyCache()
    cache.set_cache(key='offline-user',value=LiteLLM_UserTable(user_id='offline-user',user_role='internal_user'),model_type=LiteLLM_UserTable)
    monkeypatch.setattr(server,'user_api_key_cache',cache)
    router=litellm.Router(model_list=[
        {'model_name':name,'litellm_params':{'model':'openai/gpt-4o-mini','api_key':CANARY}}
        for name in ['gpt-4o-mini','chatgpt/gpt-6-luna','private/denied']
    ],num_retries=0)
    monkeypatch.setattr(server,'llm_router',router)
    monkeypatch.setattr(server,'llm_model_list',router.model_list)
    return server.app

def seed_virtual(monkeypatch, models, **extra):
    from litellm.proxy.utils import hash_token
    token=UserAPIKeyAuth(api_key=hash_token(VIRTUAL),token=hash_token(VIRTUAL),
                        user_role=LitellmUserRoles.INTERNAL_USER,models=models,user_id='offline-user',**extra)
    server.user_api_key_cache.set_cache(key=hash_token(VIRTUAL),value=token,model_type=UserAPIKeyAuth)
    # All required rows are already in the real memory cache. No database I/O allowed.
    monkeypatch.setattr(server,'prisma_client',object())

async def get(app,path,token=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://offline.invalid') as client:
        result=await client.get(path,headers={'Authorization':'Bearer '+token} if token else {})
        assert CANARY not in result.text and MASTER not in result.text and VIRTUAL not in result.text
        return result

@pytest.mark.parametrize('prefix',['/v1/models/','/models/'])
@pytest.mark.parametrize('name',['gpt-4o-mini','chatgpt/gpt-6-luna','chatgpt%2Fgpt-6-luna'])
@pytest.mark.parametrize('identity',['admin','permitted_virtual'])
def test_model_retrieval(proxy, monkeypatch, prefix, name, identity):
    if identity=='permitted_virtual':
        seed_virtual(monkeypatch,['gpt-4o-mini','chatgpt/gpt-6-luna'])
    result=asyncio.run(get(proxy,prefix+name, MASTER if identity=='admin' else VIRTUAL))
    assert result.status_code==200
    assert result.json()['id']==name.replace('%2F','/')
    assert result.json()['object']=='model'
    assert 'litellm_params' not in result.json()

@pytest.mark.parametrize('name',['gpt-4o-mini','chatgpt/gpt-6-luna','chatgpt%2Fgpt-6-luna','missing/model'])
def test_unauthenticated_model_is_401(proxy,name):
    result=asyncio.run(get(proxy,'/v1/models/'+name))
    assert result.status_code==401
    assert isinstance(result.json()['error'],dict)

@pytest.mark.parametrize('name',['missing','missing/model','private/denied','private%2Fdenied'])
def test_missing_or_forbidden_returns_404(proxy, monkeypatch, name):
    seed_virtual(monkeypatch,['chatgpt/gpt-6-luna'])
    result=asyncio.run(get(proxy,'/v1/models/'+name,VIRTUAL))
    assert result.status_code==404
    assert result.json().get('object')!='model'


def logging_fixture(cost=None):
    obj=Logging(model='gpt-4o-mini',messages=[],stream=False,call_type='acompletion',
        start_time=datetime.datetime.now(),litellm_call_id='offline-call',function_id='offline-fn')
    if cost is not None: obj.model_call_details['response_cost']=cost
    obj._response_cost_calculator=Mock(return_value=cost)
    return obj

@pytest.mark.parametrize('cost',[0,0.0,'0',0.125,None])
def test_stream_header_preserves_known_zero(cost):
    log=logging_fixture()
    headers=Processing({})._stream_response_headers(hidden_params={'response_cost':cost},
        user_api_key_dict=UserAPIKeyAuth(),logging_obj=log,version='1.104.0',callback_headers={})
    if cost is None: assert 'x-litellm-response-cost' not in headers
    else: assert float(headers['x-litellm-response-cost'])==float(cost)

@pytest.mark.parametrize('source',['hidden','logging','calculator','unknown'])
@pytest.mark.parametrize('cost',[0,0.0,0.125])
@pytest.mark.parametrize('shape',['chat','messages','responses'])
def test_real_processing_cost_header(monkeypatch,source,cost,shape):
    """Run the full processing tail, not just get_custom_headers with a literal zero."""
    import litellm.proxy.common_request_processing as module
    log=logging_fixture(cost if source in ('logging','calculator') else None)
    if source=='calculator': log.model_call_details.pop('response_cost',None)
    response=ModelResponse(model='gpt-4o-mini',choices=[{'index':0,'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}])
    if shape=='responses':
        from litellm.types.llms.openai import ResponsesAPIResponse
        response=ResponsesAPIResponse(id='resp_offline',object='response',created_at=1,status='completed',
            model='gpt-4o-mini',output=[],usage={'input_tokens':1,'output_tokens':1,'total_tokens':2})
    if source=='hidden': response._hidden_params['response_cost']=cost
    if shape=='messages':
        response={'id':'msg_offline','type':'message','role':'assistant','model':'gpt-4o-mini','content':[{'type':'text','text':'OK'}],
                  'stop_reason':'end_turn','usage':{'input_tokens':1,'output_tokens':1}}
        if source=='hidden': response['_hidden_params']={'response_cost':cost}
    async def route(**kwargs):
        async def provider(): return response
        return provider()
    monkeypatch.setattr(module,'route_request',route)
    hooks=Mock()
    hooks.during_call_hook=AsyncMock()
    hooks.update_request_status=AsyncMock()
    hooks.post_call_success_hook=AsyncMock(side_effect=lambda **kw:kw['response'])
    hooks.post_call_response_headers_hook=AsyncMock(return_value={})
    # Disable unrelated deferred telemetry; preserve the entire cost extraction/recovery/header path.
    monkeypatch.setattr(Processing,'_flush_deferred_async_logging',staticmethod(lambda **kw:None))
    out=Response()
    processing=Processing({'model':'gpt-4o-mini','litellm_logging_obj':log})
    async def run():
        await processing._process_llm_request(request=Request({'type':'http','method':'POST','path':'/v1/chat/completions','headers':[]}),
            fastapi_response=out,user_api_key_dict=UserAPIKeyAuth(),route_type={'messages':'anthropic_messages','responses':'aresponses','chat':'acompletion'}[shape],
            proxy_logging_obj=hooks,general_settings={},proxy_config=Mock(),version='1.104.0',skip_pre_call_logic=True)
    asyncio.run(run())
    if source=='unknown': assert 'x-litellm-response-cost' not in out.headers
    else: assert float(out.headers['x-litellm-response-cost'])==cost
    assert CANARY not in str(out.headers)

@pytest.mark.parametrize('prefix',['/v1/models','/models'])
def test_listing_remains_reachable(proxy,prefix):
    result=asyncio.run(get(proxy,prefix,MASTER))
    assert result.status_code==200 and result.json()['object']=='list'
    assert 'chatgpt/gpt-6-luna' in {m['id'] for m in result.json()['data']}

@pytest.mark.parametrize('path',['/v1/models/chatgpt/gpt-6-luna','/models/chatgpt%2Fgpt-6-luna'])
def test_virtual_key_route_restriction_not_bypassed(proxy,monkeypatch,path):
    seed_virtual(monkeypatch,['chatgpt/gpt-6-luna'],allowed_routes=['/v1/chat/completions'])
    result=asyncio.run(get(proxy,path,VIRTUAL))
    assert result.status_code==403
    assert result.json().get('object')!='model'

@pytest.mark.parametrize('path',['/v1/models/missing/model','/models/missing/model'])
def test_admin_nonexistent_slash_404(proxy,path):
    result=asyncio.run(get(proxy,path,MASTER))
    assert result.status_code==404 and result.json().get('object')!='model'

@pytest.mark.parametrize('configured',[None,False,True])
@pytest.mark.parametrize('premium',[False,True])
def test_ai_hub_config_read_disposition(proxy,monkeypatch,configured,premium):
    from litellm.proxy.config_resolvers.settings_store import SettingsStore
    settings=SettingsStore('general_settings')
    if configured is not None: settings.load_yaml({'enable_public_model_hub':configured})
    monkeypatch.setattr(server.proxy_config,'settings',settings)
    monkeypatch.setattr(server,'prisma_client',object())
    monkeypatch.setattr(server,'premium_user',premium)
    result=asyncio.run(get(proxy,'/config/field/info?field_name=enable_public_model_hub',MASTER))
    if configured is None:
        assert result.status_code==400
        assert 'Field name=enable_public_model_hub is not set' in result.text
    else:
        assert result.status_code==200
        assert result.json()['field_value'] is configured
        assert result.json()['source']=='config'
        assert result.json()['editable'] is False
