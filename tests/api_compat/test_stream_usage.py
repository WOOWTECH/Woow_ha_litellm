"""Real Responses translator -> CustomStreamWrapper -> Pydantic wire JSON."""
import asyncio
import datetime
from unittest.mock import AsyncMock, Mock
import pytest
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.completion_extras.litellm_responses_transformation.transformation import OpenAiResponsesToChatCompletionStreamIterator


def native_events(tool=False, incomplete=False):
    events = [{'type':'response.created','response':{'id':'resp_offline','created_at':1}}]
    if tool:
        events += [
            {'type':'response.output_item.added','output_index':0,'item':{'type':'function_call','id':'fc_offline','call_id':'call_offline','name':'weather','arguments':''}},
            {'type':'response.function_call_arguments.delta','output_index':0,'item_id':'fc_offline','delta':'{"city":"Taipei"}'},
        ]
        output=[{'type':'function_call','id':'fc_offline','call_id':'call_offline','name':'weather','arguments':'{"city":"Taipei"}'}]
    else:
        events += [{'type':'response.output_text.delta','output_index':0,'content_index':0,'item_id':'msg_offline','delta':'OK'}]
        output=[{'type':'message','id':'msg_offline','role':'assistant','status':'completed','content':[{'type':'output_text','text':'OK','annotations':[]}]}]
    terminal={'id':'resp_offline','status':'incomplete' if incomplete else 'completed','output':output,
              'usage':{'input_tokens':4,'output_tokens':2,'total_tokens':6}}
    if incomplete: terminal['incomplete_details']={'reason':'max_output_tokens'}
    events.append({'type':'response.incomplete' if incomplete else 'response.completed','response':terminal})
    return events


def make_wrapper(asynchronous, include_usage, tool=False, incomplete=False, provider='chatgpt'):
    # Translation is the actual provider bridge, not a mocked chunk creator.
    translator=OpenAiResponsesToChatCompletionStreamIterator(iter(()),sync_stream=True)
    chunks=[translator.chunk_parser(e) for e in native_events(tool,incomplete)]
    for chunk in chunks:
        chunk._hidden_params['api_key']='offline-stream-secret-sentinel'
    async def upstream():
        for chunk in chunks: yield chunk
    log=Logging(model='gpt-6-luna',messages=[{'role':'user','content':'offline fixture'}],stream=True,
                call_type='acompletion' if asynchronous else 'completion',start_time=datetime.datetime.now(),
                litellm_call_id='offline-call',function_id='offline-function')
    log.model_call_details['custom_llm_provider']=provider
    log.success_handler=Mock()
    log.dispatch_success_handlers=AsyncMock()
    log.dispatch_failure_handlers=AsyncMock()
    wrapper=CustomStreamWrapper(completion_stream=upstream() if asynchronous else iter(chunks),
        model='gpt-6-luna',logging_obj=log,custom_llm_provider=provider,
        stream_options={'include_usage':include_usage})
    # Background cache/telemetry are external boundaries, not transformation seams.
    wrapper.async_cache_streaming_response=AsyncMock()
    wrapper.run_success_logging_and_cache_storage=Mock()
    return wrapper

@pytest.mark.parametrize('asynchronous',[False,True])
@pytest.mark.parametrize('include_usage',[False,True])
@pytest.mark.parametrize('kind',['text','tool','incomplete'])
def test_native_chat_stream_contract(asynchronous, include_usage, kind):
    wrapper=make_wrapper(asynchronous,include_usage,kind=='tool',kind=='incomplete')
    async def consume(): return [c async for c in wrapper]
    chunks=asyncio.run(consume()) if asynchronous else list(wrapper)
    wire=[c.model_dump(exclude_none=True) for c in chunks]
    usage=[c for c in wire if c.get('usage')]
    assert len(usage)==int(include_usage)
    choices=[choice for c in wire for choice in c['choices']]
    assert sum(c.get('finish_reason') is not None for c in choices)==1
    expected='length' if kind=='incomplete' else 'tool_calls' if kind=='tool' else 'stop'
    assert choices[-1].get('finish_reason')==expected
    assert len({c['id'] for c in wire})==1
    assert len({c['created'] for c in wire})==1
    assert all(c['object']=='chat.completion.chunk' for c in wire)
    if kind=='tool':
        calls=[t for c in choices for t in c['delta'].get('tool_calls',[])]
        assert calls and all(t['index']==0 for t in calls)
        assert ''.join(t['function'].get('arguments','') for t in calls)=='{"city":"Taipei"}'
    else:
        assert ''.join(c['delta'].get('content','') or '' for c in choices)=='OK'
    if include_usage:
        assert wire[-1] is usage[0]
        assert usage[0]['choices']==[]
        assert usage[0]['usage']['prompt_tokens']==4
        assert usage[0]['usage']['completion_tokens']==2
        assert usage[0]['usage']['total_tokens']==6

@pytest.mark.parametrize('include_usage',[False,True])
@pytest.mark.parametrize('kind',['text','tool','incomplete'])
def test_actual_proxy_sse_serialization(monkeypatch,include_usage,kind):
    """Actual proxy serializer must preserve [] and emit exactly one [DONE]."""
    import json
    from litellm.proxy import proxy_server as server
    from litellm.proxy._types import UserAPIKeyAuth
    hooks=Mock()
    hooks.needs_iterator_wrap=Mock(return_value=False)
    hooks.needs_per_chunk_streaming_hook=Mock(return_value=False)
    hooks.post_call_success_hook=AsyncMock()
    monkeypatch.setattr(server,'proxy_logging_obj',hooks)
    monkeypatch.setattr(server,'llm_router',None)
    wrapper=make_wrapper(True,include_usage,kind=='tool',kind=='incomplete')
    async def consume():
        return [frame async for frame in server.async_data_generator(
            response=wrapper,user_api_key_dict=UserAPIKeyAuth(),
            request_data={'model':'chatgpt/gpt-6-luna','stream_options':{'include_usage':include_usage}})]
    frames=asyncio.run(consume())
    frames=[f.decode('utf-8') if isinstance(f,bytes) else f for f in frames]
    data=[line[6:] for frame in frames for line in frame.splitlines() if line.startswith('data: ')]
    assert data[-1]=='[DONE]' and data.count('[DONE]')==1
    packets=[json.loads(s) for s in data[:-1]]
    assert not any('error' in p for p in packets)
    assert 'offline-stream-secret-sentinel' not in ''.join(frames)
    assert '_hidden_params' not in ''.join(frames)
    assert all(p['model']=='chatgpt/gpt-6-luna' for p in packets)
    usage=[p for p in packets if p.get('usage')]
    assert len(usage)==int(include_usage)
    if include_usage:
        assert packets[-1]['choices']==[] and packets[-1]['usage']['total_tokens']==6
    assert sum(bool(c.get('finish_reason')) for p in packets for c in p['choices'])==1

@pytest.mark.parametrize('asynchronous',[False,True])
@pytest.mark.parametrize('include_usage',[False,True])
def test_openai_provider_usage_regression(asynchronous,include_usage):
    wrapper=make_wrapper(asynchronous,include_usage,provider='openai')
    async def consume(): return [c async for c in wrapper]
    chunks=asyncio.run(consume()) if asynchronous else list(wrapper)
    usage=[c for c in chunks if getattr(c,'usage',None) is not None]
    assert len(usage)==int(include_usage)
    assert ''.join(c.delta.content or '' for chunk in chunks for c in chunk.choices)=='OK'
    if include_usage:
        assert chunks[-1].choices==[] and chunks[-1].usage.total_tokens==6
