"""Bounded compatibility patch for the pinned LiteLLM 1.104.0 image.

ChatGPT requires SSE upstream even for non-stream clients. Keep that wire body,
but select the buffered parser for non-stream callers so its output recovery runs.
Fail before modifying anything when the reviewed upstream source has changed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

PATCHES = {
    'llms/chatgpt/responses/transformation.py': (
        'b1a66c0e15bb882ee394273ca8e24854327f33b2f9d3e4bcc506c48c4e19ed85',
        '        request: Final = super().transform_responses_api_request(\n',
        '        # ChatGPT accepts a message array; the public Responses API also accepts text.\n'
        '        if isinstance(input, str):\n'
        '            input = [{"role": "user", "content": input}]\n'
        '        request: Final = super().transform_responses_api_request(\n',
        1,
    ),
    'llms/custom_httpx/llm_http_handler.py': (
        'a7958b892ba4b309e451297d87be4c0bb341f7e56f297cb710ceee1539a53c8a',
        '        stream = bool(stream or data.get("stream"))\n',
        '        # ChatGPT forces SSE in its upstream body, not in the client response.\n'
        '        # Non-stream callers use its buffered SSE parser and output recovery.\n'
        '        if custom_llm_provider != "chatgpt":\n'
        '            stream = bool(stream or data.get("stream"))\n',
        2,
    ),
}

def apply(root):
    pending=[]
    for name,(expected,old,new,count) in PATCHES.items():
        path=root/name
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected:
            raise SystemExit('Refusing changed upstream source: '+name)
        source=raw.decode()
        if source.count(old)!=count:
            raise SystemExit('Refusing changed patch context: '+name)
        result=source.replace(old,new)
        compile(result,str(path),'exec')
        pending.append((path,result))
    for path,result in pending:
        path.write_text(result)
        print(path.name+': '+hashlib.sha256(path.read_bytes()).hexdigest())

if __name__=='__main__':
    root=Path(sys.argv[1]) if len(sys.argv)>1 else Path(next(iter(importlib.util.find_spec('litellm').submodule_search_locations)))
    apply(root)
