"""Offline checks against the image's real LiteLLM HTTP/provider/bridge implementation."""
import os
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
import asyncio
import datetime
import json
import unittest
from unittest.mock import patch

import httpx
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.llms.custom_httpx.http_handler import HTTPHandler, AsyncHTTPHandler
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.types.router import GenericLiteLLMParams
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.utils import ModelResponse
from litellm.completion_extras.litellm_responses_transformation.transformation import LiteLLMResponsesTransformationHandler

ITEM = {'type':'message','id':'msg_test','role':'assistant','status':'completed',
        'content':[{'type':'output_text','text':'OK','annotations':[]}]}

def payload(output):
    return {'id':'resp_test','object':'response','created_at':1,'model':'gpt-6-luna',
            'status':'completed','output':output,'error':None,'incomplete_details':None,
            'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}

def sse(empty=False, failure=False):
    events = [
        {'type':'response.output_text.done','output_index':0,'content_index':0,'item_id':'msg_test','text':'OK'},
        {'type':'response.output_item.done','output_index':0,'item':ITEM},
        {'type':'response.completed','response':payload([] if empty else [ITEM])},
    ]
    if failure:
        events = [{'type':'response.failed','response':{'error':{'message':'synthetic provider failure'}}}]
    return ''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'

class ResponsesContract(unittest.TestCase):
    def exercise(self, *, asynchronous=False, stream=False, text_input=True,
                 empty=False, failure=False, provider='chatgpt', extra_body=None):
        seen=[]
        def serve(request):
            body=json.loads(request.content); seen.append(body)
            if provider=='chatgpt':
                self.assertIs(body['stream'], True)
                self.assertIsInstance(body['input'],list)
                return httpx.Response(200, text=sse(empty,failure), headers={'content-type':'text/event-stream'})
            return httpx.Response(200,json=payload([ITEM]))
        transport=httpx.MockTransport(serve)
        log=Logging(model='gpt-6-luna',messages=[],stream=stream,call_type='responses',
                    start_time=datetime.datetime.now(),litellm_call_id='offline-test',function_id='offline')
        conf=ChatGPTResponsesAPIConfig() if provider=='chatgpt' else OpenAIResponsesAPIConfig()
        args=dict(model='gpt-6-luna', input='Say OK' if text_input else [{'role':'user','content':'Say OK'}],
                  responses_api_provider_config=conf,response_api_optional_request_params={'stream':stream},
                  custom_llm_provider=provider,litellm_params=GenericLiteLLMParams(api_base='https://offline.invalid/v1',api_key='test'),
                  logging_obj=log,timeout=5,extra_body=extra_body)
        handler=BaseLLMHTTPHandler()
        with patch.object(type(conf),'validate_environment',return_value={}):
            if asynchronous:
                async def run():
                    client=AsyncHTTPHandler(transport=transport)
                    try:
                        result=await handler.async_response_api_handler(**args,client=client)
                        if stream:
                            events=[event async for event in result]
                            self.assertTrue(events)
                            return result
                        return result
                    finally:
                        await client.client.aclose()
                result=asyncio.run(run())
            else:
                with httpx.Client(transport=transport) as http:
                    result=handler.response_api_handler(**args,client=HTTPHandler(client=http))
                    if stream:
                        self.assertTrue(list(result))
        self.assertEqual(len(seen),1)
        if stream:
            self.assertNotIsInstance(result,ResponsesAPIResponse)
        else:
            self.assertIsInstance(result,ResponsesAPIResponse)
            self.assertEqual(result.output[0].content[0].text,'OK')
            bridge=LiteLLMResponsesTransformationHandler().transform_response(
                model='gpt-6-luna',raw_response=result,model_response=ModelResponse(),logging_obj=log,
                request_data={},messages=[{'role':'user','content':'Say OK'}],optional_params={},litellm_params={},encoding=None)
            self.assertEqual(bridge.choices[0].message.content,'OK')
        return seen

    def test_sync_string_nonstream(self): self.exercise()
    def test_async_string_nonstream(self): self.exercise(asynchronous=True)
    def test_sync_array_nonstream(self): self.exercise(text_input=False)
    def test_async_array_nonstream(self): self.exercise(asynchronous=True,text_input=False)
    def test_sync_empty_terminal_recovers_output(self): self.exercise(empty=True)
    def test_async_empty_terminal_recovers_output(self): self.exercise(asynchronous=True,empty=True)
    def test_sync_stream(self): self.exercise(stream=True)
    def test_async_stream(self): self.exercise(asynchronous=True,stream=True)
    def test_other_provider_nonstream(self): self.exercise(provider='openai')
    def test_other_provider_async_nonstream(self): self.exercise(provider='openai',asynchronous=True)
    def test_provider_failure_not_success(self):
        with self.assertRaisesRegex(Exception,'synthetic provider failure'): self.exercise(failure=True)

if __name__=='__main__': unittest.main(verbosity=2)
