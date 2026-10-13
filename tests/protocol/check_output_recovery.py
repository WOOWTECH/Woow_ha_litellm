"""Synthetic SSE through real HTTP provider, iterator, collector and chat bridge."""
import copy
import json
import socket
import subprocess
import unittest

from check_responses import (
    ITEM, ResponsesContract, payload, LiteLLMResponsesTransformationHandler, ModelResponse,
)

TOOL = {'type': 'function_call', 'id': 'fc_fixture', 'call_id': 'call_fixture',
        'name': 'fixture_lookup', 'arguments': '{"key":"value"}', 'status': 'completed'}


def wire(events, *, output=None, status='completed', terminal=True):
    events = copy.deepcopy(events)
    if terminal:
        response = payload([] if output is None else output)
        response['status'] = status
        if status == 'incomplete':
            response['incomplete_details'] = {'reason': 'max_output_tokens'}
        events.append({'type': 'response.' + status, 'response': response})
    for index, event in enumerate(events):
        event['sequence_number'] = index
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + 'data: [DONE]\n\n'


def text_event(kind, text, output_index=0, content_index=0):
    event = {'type': 'response.output_text.' + kind, 'output_index': output_index,
             'content_index': content_index, 'item_id': 'msg_test',
             'delta' if kind == 'delta' else 'text': text}
    if kind == 'delta':
        event['logprobs'] = []
    return event


def item_event(item=ITEM, index=0):
    return {'type': 'response.output_item.done', 'output_index': index, 'item': item}


class OutputRecoveryContract(unittest.TestCase):
    def run_paths(self, body, check):
        for asynchronous in (False, True):
            for collect in (False, True):
                with self.subTest(asynchronous=asynchronous, collect=collect):
                    ResponsesContract().exercise(asynchronous=asynchronous, stream=collect,
                        collect=collect, wire=body, check_response=check)

    def bridge(self, response, log):
        return LiteLLMResponsesTransformationHandler().transform_response(
            model='gpt-6-luna', raw_response=response, model_response=ModelResponse(),
            logging_obj=log, request_data={}, messages=[{'role':'user','content':'Say OK'}],
            optional_params={}, litellm_params={}, encoding=None)

    def text_is(self, expected, reason='stop'):
        def check(response, log):
            result = self.bridge(response, log)
            self.assertEqual(result.choices[0].message.content, expected)
            self.assertEqual(result.choices[0].finish_reason, reason)
        return check

    def test_delta_only_completed_preserves_text(self):
        self.run_paths(wire([text_event('delta', 'O'), text_event('delta', 'K')]), self.text_is('OK'))

    def test_text_done_only_completed_preserves_text(self):
        self.run_paths(wire([text_event('done', 'OK')]), self.text_is('OK'))

    def test_item_done_only_completed_preserves_text(self):
        self.run_paths(wire([item_event()]), self.text_is('OK'))

    def test_completed_payload_only_preserves_text(self):
        self.run_paths(wire([], output=[ITEM]), self.text_is('OK'))

    def test_authoritative_done_and_completed_do_not_duplicate_deltas(self):
        events = [text_event('delta', 'stale'), text_event('done', 'OK'), item_event()]
        self.run_paths(wire(events), self.text_is('OK'))
        final = copy.deepcopy(ITEM)
        final['content'][0]['text'] = 'terminal'
        self.run_paths(wire(events, output=[final]), self.text_is('terminal'))

    def test_incomplete_keeps_partial_and_length(self):
        self.run_paths(wire([text_event('delta', 'partial')], status='incomplete'), self.text_is('partial', 'length'))

    def test_tool_only_retains_arguments_without_fabricating_text(self):
        def check(response, log):
            result = self.bridge(response, log)
            self.assertEqual(result.choices[0].finish_reason, 'tool_calls')
            self.assertIn(result.choices[0].message.content, (None, ''))
            calls = result.choices[0].message.tool_calls
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].function.name, TOOL['name'])
            self.assertEqual(calls[0].function.arguments, TOOL['arguments'])
            self.assertEqual(calls[0].id, TOOL['call_id'])
        self.run_paths(wire([item_event(TOOL)]), check)

    def test_genuinely_empty_completed_is_still_error(self):
        def check(response, log):
            self.assertEqual(response.output, [])
            with self.assertRaisesRegex(ValueError, 'Unknown.*items'):
                self.bridge(response, log)
        self.run_paths(wire([]), check)

    def test_error_after_partial_is_not_success(self):
        events = [text_event('delta', 'partial'), {'type': 'error', 'code': 'rate_limit_exceeded',
                  'message': 'synthetic provider failure', 'param': None}]
        for asynchronous in (False, True):
            for collect in (False, True):
                with self.subTest(asynchronous=asynchronous, collect=collect):
                    with self.assertRaises(Exception) as caught:
                        ResponsesContract().exercise(asynchronous=asynchronous, stream=collect,
                            collect=collect, wire=wire(events, terminal=False))
                    self.assertEqual(caught.exception.status_code, 429)

    def test_partial_without_terminal_is_not_success(self):
        for asynchronous in (False, True):
            for collect in (False, True):
                with self.subTest(asynchronous=asynchronous, collect=collect):
                    with self.assertRaises(Exception) as caught:
                        ResponsesContract().exercise(asynchronous=asynchronous, stream=collect,
                            collect=collect, wire=wire([text_event('delta', 'partial')], terminal=False))
                    if collect:
                        self.assertIsInstance(caught.exception, ValueError)
                        self.assertIn('without a completed response', str(caught.exception))
                    else:
                        self.assertEqual(caught.exception.status_code, 502)
                    self.assertNotIn('data:', str(caught.exception))

    def test_interleaved_output_slots_preserve_order(self):
        events = [text_event('delta', 'B', output_index=1),
                  text_event('delta', 'A', output_index=0),
                  text_event('delta', '2', output_index=1),
                  text_event('delta', '1', output_index=0)]
        def check(response, log):
            self.assertEqual([item.content[0].text for item in response.output], ['A1', 'B2'])
            self.assertEqual(response.usage.input_tokens, 1)
            self.assertEqual(response.usage.output_tokens, 1)
            self.assertEqual(response.status, 'completed')
        self.run_paths(wire(events), check)

    def test_late_delta_cannot_overwrite_done(self):
        events = [text_event('delta', 'stale'), text_event('done', 'OK'), text_event('delta', 'late')]
        self.run_paths(wire(events), self.text_is('OK'))

    def test_empty_incomplete_retains_length_without_fake_content(self):
        self.run_paths(wire([], status='incomplete'), self.text_is('', 'length'))

    def test_failed_terminal_after_text_is_not_success(self):
        events = [item_event(), {'type':'response.failed', 'response': {
            **payload([]), 'status':'failed', 'error': {
                'code':'rate_limit_exceeded', 'message':'synthetic provider failure'}}}]
        for asynchronous in (False, True):
            for collect in (False, True):
                with self.subTest(asynchronous=asynchronous, collect=collect):
                    with self.assertRaises(Exception) as caught:
                        ResponsesContract().exercise(asynchronous=asynchronous, stream=collect,
                            collect=collect, wire=wire(events, terminal=False))
                    self.assertEqual(caught.exception.status_code, 429)

    def test_public_completion_uses_real_provider_parser(self):
        import asyncio
        from unittest.mock import patch
        import httpx
        import litellm
        from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
        from litellm.llms.custom_httpx.http_handler import HTTPHandler, AsyncHTTPHandler
        body = wire([text_event('delta', 'O'), text_event('delta', 'K')])
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                seen = []
                def serve(request):
                    request_body = json.loads(request.content)
                    seen.append(request_body)
                    self.assertIs(request_body['stream'], True)
                    self.assertNotIn('max_tokens', request_body)
                    self.assertNotIn('max_output_tokens', request_body)
                    return httpx.Response(200, text=body, headers={'content-type':'text/event-stream'})
                transport = httpx.MockTransport(serve)
                kwargs = dict(model='chatgpt/gpt-6-luna', messages=[{'role':'user','content':'Say OK'}],
                              stream=False, max_tokens=64, num_retries=0)
                with patch.dict(litellm.model_cost, {'chatgpt/gpt-6-luna': {
                        'mode':'responses', 'litellm_provider':'chatgpt', 'max_tokens':10000,
                        'input_cost_per_token':0, 'output_cost_per_token':0}}), \
                     patch.object(ChatGPTResponsesAPIConfig, 'validate_environment', return_value={}), \
                     patch.object(ChatGPTResponsesAPIConfig, 'get_complete_url', return_value='https://offline.invalid/responses'):
                    if asynchronous:
                        async def run():
                            client = AsyncHTTPHandler(transport=transport)
                            try:
                                with patch('litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client', return_value=client):
                                    return await litellm.acompletion(**kwargs, client=client)
                            finally:
                                await client.client.aclose()
                        result = asyncio.run(run())
                    else:
                        with httpx.Client(transport=transport) as http:
                            result = litellm.completion(**kwargs, client=HTTPHandler(client=http))
                self.assertEqual(len(seen), 1)
                self.assertEqual(result.choices[0].message.content, 'OK')
                self.assertEqual(result.choices[0].finish_reason, 'stop')

    def test_interleaved_content_slots_preserve_order(self):
        events = [text_event('delta', 'second', content_index=1),
                  text_event('delta', 'first', content_index=0)]
        def check(response, log):
            self.assertEqual([part.text for part in response.output[0].content], ['first', 'second'])
        self.run_paths(wire(events), check)

    def test_reasoning_only_does_not_invent_assistant_message(self):
        reasoning = {'id':'rs_fixture', 'type':'reasoning', 'summary':[]}
        def check(response, log):
            self.assertEqual(len(response.output), 1)
            self.assertEqual(response.output[0].type, 'reasoning')
            with self.assertRaisesRegex(ValueError, 'Unknown.*items'):
                self.bridge(response, log)
        self.run_paths(wire([item_event(reasoning)]), check)

    def test_other_provider_collector_remains_unchanged(self):
        def check(response, log):
            self.assertEqual(response.output, [])
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                ResponsesContract().exercise(asynchronous=asynchronous, stream=True,
                    collect=True, provider='openai', wire=wire([item_event()]), check_response=check)

    def test_public_empty_response_remains_failure_without_retry(self):
        import litellm
        import httpx
        from unittest.mock import patch
        from litellm.llms.custom_httpx.http_handler import HTTPHandler
        from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
        seen = []
        def serve(request):
            seen.append(request)
            return httpx.Response(200, text=wire([]), headers={'content-type':'text/event-stream'})
        with patch.dict(litellm.model_cost, {'chatgpt/gpt-6-luna': {
                'mode':'responses', 'litellm_provider':'chatgpt', 'max_tokens':10000,
                'input_cost_per_token':0, 'output_cost_per_token':0}}), \
             patch.object(ChatGPTResponsesAPIConfig, 'validate_environment', return_value={}), \
             patch.object(ChatGPTResponsesAPIConfig, 'get_complete_url', return_value='https://offline.invalid/responses'), \
             httpx.Client(transport=httpx.MockTransport(serve)) as http:
            with self.assertRaises(Exception) as caught:
                litellm.completion(model='chatgpt/gpt-6-luna', messages=[{'role':'user','content':'Say OK'}],
                    stream=False, num_retries=0, client=HTTPHandler(client=http))
            self.assertIn('Unknown', str(caught.exception))
            self.assertEqual(len(seen), 1)

    def mixed_tool_events(self, *, added=True, argument_done=True):
        tool = {**TOOL, 'status':'in_progress', 'arguments':''}
        events = [text_event('delta', 'I will look that up.')]
        if added:
            events.append({'type':'response.output_item.added', 'output_index':1, 'item':tool})
        events.append({'type':'response.function_call_arguments.delta', 'output_index':1,
                       'item_id':TOOL['id'], 'delta':TOOL['arguments']})
        if argument_done:
            events.append({'type':'response.function_call_arguments.done', 'output_index':1,
                           'item_id':TOOL['id'], 'arguments':TOOL['arguments'], 'name':TOOL['name']})
        return events

    def assert_unresolved_tool_refused(self, events, status='completed'):
        for asynchronous in (False, True):
            for collect in (False, True):
                with self.subTest(asynchronous=asynchronous, collect=collect, status=status):
                    with self.assertRaisesRegex(Exception, 'unresolved tool output') as caught:
                        ResponsesContract().exercise(asynchronous=asynchronous, stream=collect,
                            collect=collect, wire=wire(events, status=status),
                            check_response=lambda response, log: self.bridge(response, log))
                    self.assertNotIn('I will look that up.', str(caught.exception))
                    self.assertNotIn(TOOL['arguments'], str(caught.exception))

    def test_mixed_text_tool_without_item_done_refuses_partial_success(self):
        self.assert_unresolved_tool_refused(self.mixed_tool_events())

    def test_mixed_text_tool_missing_argument_done_refuses_partial_success(self):
        self.assert_unresolved_tool_refused(self.mixed_tool_events(argument_done=False))

    def test_tool_argument_events_without_added_refuse_partial_success(self):
        for argument_done in (False, True):
            with self.subTest(argument_done=argument_done):
                self.assert_unresolved_tool_refused(self.mixed_tool_events(added=False, argument_done=argument_done))

    def test_added_tool_without_arguments_refuses_partial_success(self):
        self.assert_unresolved_tool_refused(self.mixed_tool_events()[:2])

    def test_incomplete_with_unresolved_tool_refuses_partial_success(self):
        self.assert_unresolved_tool_refused(self.mixed_tool_events(argument_done=False), status='incomplete')

    def test_unrelated_done_item_cannot_resolve_observed_tool(self):
        for wrong_tool, index in [({**TOOL, 'id':'fc_other'}, 1), (TOOL, 2)]:
            with self.subTest(index=index, item_id=wrong_tool['id']):
                self.assert_unresolved_tool_refused(self.mixed_tool_events() + [item_event(wrong_tool, index)])

    def test_overwritten_tool_item_cannot_allow_partial_recovery(self):
        events = self.mixed_tool_events() + [item_event(TOOL, 1), item_event(ITEM, 1)]
        self.assert_unresolved_tool_refused(events)

    def assert_mixed_tool_preserved(self, response, log):
        self.assertEqual([item.type for item in response.output], ['message', 'function_call'])
        result = self.bridge(response, log)
        # Upstream emits text and accumulated tool calls as separate choices.
        self.assertEqual([choice.finish_reason for choice in result.choices], ['stop', 'tool_calls'])
        self.assertEqual(result.choices[0].message.content, response.output[0].content[0].text)
        self.assertEqual(len(result.choices[1].message.tool_calls), 1)
        call = result.choices[1].message.tool_calls[0]
        self.assertEqual(call.id, TOOL['call_id'])
        self.assertEqual(call.function.name, TOOL['name'])
        self.assertEqual(call.function.arguments, TOOL['arguments'])

    def test_matching_item_done_preserves_mixed_tool_without_argument_done(self):
        events = self.mixed_tool_events(argument_done=False) + [item_event(TOOL, 1)]
        self.run_paths(wire(events), self.assert_mixed_tool_preserved)

    def test_authoritative_terminal_output_wins_over_unresolved_tool(self):
        events = self.mixed_tool_events(argument_done=False)
        self.run_paths(wire(events, output=[ITEM, TOOL]), self.assert_mixed_tool_preserved)
        # An authoritative text-only terminal is also left untouched; the
        # refusal applies only when rebuilding an empty terminal from events.
        self.run_paths(wire(events, output=[ITEM]), self.text_is('OK'))

    def test_offline_network_and_process_guards(self):
        with self.assertRaises(PermissionError):
            socket.getaddrinfo('offline.invalid', 443)
        with self.assertRaises(PermissionError):
            subprocess.run(['must-not-start-offline-fixture'], check=True)
