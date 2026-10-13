# LiteLLM 1.104.0 ChatGPT Responses compatibility

HA acceptance on 2026-10-13 reproduced three failures with connected subscription accounts:

- `/v1/responses` rejects string input while the equivalent message array succeeds.
- `/v1/responses` with `stream: false` returns SSE instead of JSON.
- Nonstream Chat Completions intermittently fail with `Unknown items in responses API response: []`.

`ChatGPTResponsesAPIConfig` requires `stream: true` on the upstream wire. Both HTTP handler paths promote that provider flag to the client stream flag. The bridge then drains the streaming iterator, whose terminal response may omit output items. The buffered ChatGPT SSE parser already recovers items from earlier events.

The bounded patch converts string input to one user message and preserves the requested client stream flag for ChatGPT only. Upstream SSE remains enabled; nonstream clients use the existing buffered parser. Other providers retain their existing behavior. The two upstream file hashes must match before either file is modified. A later upstream version must be reviewed again.

`tests/protocol/check_responses.py` exercises the actual sync and async HTTP handlers, ChatGPT provider and Responses-to-Chat transformation over a mock HTTP transport. It covers string/array input, streaming, missing terminal output recovery, provider errors and the OpenAI nonstream path. Both CI and publication run it inside the built image with networking disabled.

The Woow plugin remains byte-identical to its canonical chart commit. This HA image patch has not been rolled out to PaaS; parity requires a separately reviewed chart/image change. No upstream issue has been opened. Live stability must be measured after image deployment; passing the offline regression alone does not establish it.
