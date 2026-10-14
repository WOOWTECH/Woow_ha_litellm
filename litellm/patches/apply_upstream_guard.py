"""Fail-closed build patch: upstream (OpenRouter) error leaks, token limits, budget admission.

P0  Provider errors reach clients, streaming error frames and spend logs without the gateway's
    OpenRouter account (user_id), error.metadata (raw upstream text, provider_name, is_byok,
    flagged input, headers) or request/generation ids; only rate-limit and retry-after provider
    headers are forwarded as llm_provider-*. The original text stays in the server log.
P1  max_tokens / max_completion_tokens / max_output_tokens must be null or an integer 1..2**31-1
    (OpenAI 400 envelope with param, before any provider call or spend log).
P1  Budget admission is strict (an estimate above the remaining budget is refused, not shrunk),
    reasoning models reserve at least R output tokens and a minimum input size, a negative or
    boolean max_tokens no longer shrinks the reservation, the refusal carries
    x-should-retry: false and no longer echoes the key hash, and an unpriced model used by a
    budget-limited key is logged (or refused).
P2  reasoning_effort is forwarded to OpenRouter for every model, not only models in the cost map.

Runs last, after apply_api_errors.py, on both the HA and the PaaS chains. Seven inputs are pristine
LiteLLM 1.104.0 files; proxy/common_request_processing.py is the reviewed apply_api_errors.py
output. Validate every input, replacement count, output hash, both helpers and Python syntax
before writing anything. One-shot: a second application intentionally fails closed.
"""
import hashlib
import importlib.util
from pathlib import Path
import sys

# installed path: (source file next to this installer, reviewed SHA-256)
HELPERS = {
    'litellm_core_utils/woow_provider_errors.py': (
        'woow_provider_errors.py',
        '1f623a58424faf8ac77afa82ba54f38d86e6f019381a4ef5d8a46a742bdee888',
    ),
    'proxy/woow_request_guard.py': (
        'woow_request_guard.py',
        'a62ea1fe54d8739a4b039c2e43c8258419beab52dbce9cce0ce74abb4d7acf92',
    ),
}

# relative path: (reviewed input SHA-256, reviewed output SHA-256, edits)
PATCHES = {
    'litellm_core_utils/exception_mapping_utils.py': (
        '7896a70c295d5730a975fc7edee30d91821bd9d6e09830bafd280f5a1c2da771',
        'ae43e9c4ad321c592ab96ca85540e3ec484be64dab64d790865d976ec9aca090',
        (
            (
                '            message = str(original_exception)\n'
                '\n'
                '    if message is not None and isinstance(\n'
                '        message, str\n'
                '    ):  # done to prevent user-confusion. Relevant issue - https://github.com/BerriAI/litellm/issues/1414\n',
                '            message = str(original_exception)\n'
                '    # Woow P0: OpenAI-compatible routes (openai/, text completion) pointed at OpenRouter.\n'
                '    from litellm.litellm_core_utils.woow_provider_errors import scrub_provider_error\n'
                '\n'
                '    message = scrub_provider_error(\n'
                '        custom_llm_provider, message, getattr(original_exception, "status_code", None)\n'
                '    )\n'
                '\n'
                '    if message is not None and isinstance(\n'
                '        message, str\n'
                '    ):  # done to prevent user-confusion. Relevant issue - https://github.com/BerriAI/litellm/issues/1414\n',
                1,
            ),
            (
                '            if isinstance(original_exception, BaseException):\n'
                '                exception_type = type(original_exception).__name__\n',
                '            # Woow P0: OpenRouter bodies carry the gateway account (user_id) and metadata.raw.\n'
                '            from litellm.litellm_core_utils.woow_provider_errors import scrub_provider_error\n'
                '\n'
                '            error_str = scrub_provider_error(\n'
                '                custom_llm_provider, error_str, getattr(original_exception, "status_code", None)\n'
                '            )\n'
                '            if isinstance(original_exception, BaseException):\n'
                '                exception_type = type(original_exception).__name__\n',
                1,
            ),
        ),
    ),
    'llms/openrouter/chat/transformation.py': (
        'f6cf4733c03c0b993f009dadc3fc9636434ad47cee20a4cefde2c70078578bec',
        '56e8d6f1242e7f9cd6036dcaaec2cdea620fe4c7c2566389c74184f745fcce7d',
        (
            (
                '        supported_params: Final = super().get_supported_openai_params(model=model)\n'
                '        try:\n'
                '            if litellm.supports_reasoning(model=model, custom_llm_provider="openrouter") or litellm.supports_reasoning(\n'
                '                model=model\n'
                '            ):\n'
                '                supported_params.append("reasoning_effort")\n'
                '                supported_params.append("thinking")\n',
                '        supported_params: Final = super().get_supported_openai_params(model=model)\n'
                '        # Woow P2: OpenRouter accepts reasoning_effort for any model; models missing from the\n'
                '        # cost map must not have it silently dropped (drop_params).\n'
                '        supported_params.append("reasoning_effort")\n'
                '        try:\n'
                '            if litellm.supports_reasoning(model=model, custom_llm_provider="openrouter") or litellm.supports_reasoning(\n'
                '                model=model\n'
                '            ):\n'
                '                supported_params.append("thinking")\n',
                1,
            ),
            (
                '                error_chunk: Final = chunk["error"]\n'
                '                error_message: Final = OpenRouterErrorMessage(\n'
                '                    message="Message: {}, Metadata: {}, User ID: {}".format(\n'
                '                        error_chunk["message"],\n'
                '                        error_chunk.get("metadata", {}),\n'
                '                        error_chunk.get("user_id", ""),\n'
                '                    ),\n'
                '                    code=error_chunk["code"],\n'
                '                    metadata=error_chunk.get("metadata", {}),\n'
                '                )\n'
                '                raise OpenRouterException(\n'
                '                    message=error_message["message"],\n'
                '                    status_code=error_message["code"],\n'
                '                    headers=error_message["metadata"].get("headers", {}),\n'
                '                )\n',
                '                error_chunk: Final = chunk["error"]\n'
                '                # Woow P0: no user_id, metadata or provider headers; string codes map to a status.\n'
                '                from litellm.litellm_core_utils.woow_provider_errors import stream_error_parts\n'
                '\n'
                '                message, status = stream_error_parts(error_chunk)\n'
                '                raise OpenRouterException(message=message, status_code=status, headers={})\n',
                1,
            ),
        ),
    ),
    'llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py': (
        '928654271696a04b87d3adbba9313d23fbb66cd3250fba5e3281515162a3b69b',
        'ca7fdb570f91657c486df34ca72f0e9e7153ce5adae12c27df92b7730dbf9689',
        ((
            '    if isinstance(exc, (BaseLLMException, MidStreamFallbackError)):\n'
            '        return exc.status_code, exc.message\n',
            '    if isinstance(exc, (BaseLLMException, MidStreamFallbackError)):\n'
            '        # Woow P0: second line of defence for /v1/messages mid-stream provider errors.\n'
            '        from litellm.litellm_core_utils.woow_provider_errors import client_safe_stream_message\n'
            '\n'
            '        return exc.status_code, client_safe_stream_message(exc.message)\n',
            1,
        ),),
    ),
    'litellm_core_utils/llm_response_utils/get_headers.py': (
        'fb2922ad9bccad07db19070eb08ecbe8db4aa6203bb2b36b35e09d5b02e2ce78',
        'fbae86904ca809c2908b49c9ead1fdf3cdc916d5de5bb16dfcffcc5b5d7e2de9',
        ((
            '    llm_provider_headers: Final = {}\n'
            '    for k, v in response_headers.items():\n'
            '        if "llm_provider" not in k:\n',
            '    # Woow P0: forward only rate-limit/retry-after provider headers (WOOW_PROVIDER_HEADERS=all\n'
            '    # restores every header); cf-ray, x-request-id and provider probes stay server-side.\n'
            '    from litellm.litellm_core_utils.woow_provider_errors import provider_header_allowed\n'
            '\n'
            '    llm_provider_headers: Final = {}\n'
            '    for k, v in response_headers.items():\n'
            '        if not provider_header_allowed(k):\n'
            '            continue\n'
            '        if "llm_provider" not in k:\n',
            1,
        ),),
    ),
    'litellm_core_utils/core_helpers.py': (
        '045e8d1e77e7e152da8eb21b0bb6e5dcc3e88a685b8b984357e69638dbf03d8a',
        'd5bc555486e4fc2e303b6678b4515f32b089abca028b276ddb77aff536d216c4',
        ((
            '        else:\n'
            '            additional_headers["{}-{}".format("llm_provider", k)] = v\n',
            '        else:\n'
            '            # Woow P0: streaming/raw provider headers follow the same allowlist as get_headers.\n'
            '            from litellm.litellm_core_utils.woow_provider_errors import provider_header_allowed\n'
            '\n'
            '            if provider_header_allowed(k):\n'
            '                additional_headers["{}-{}".format("llm_provider", k)] = v\n',
            1,
        ),),
    ),
    'proxy/common_request_processing.py': (
        'adc20ff67119c85e3306a9786f79dc1513cb25e1fcd23d1171a48a8a5b1e021b',
        '2184dccb1c24728a1e6ad249cfe3e1d9c8331f8d2c1bb5c93adce7e15939a117',
        ((
            '        validate_request_body(route_type, self.data)\n'
            '        self.data = await add_litellm_data_to_request(\n',
            '        validate_request_body(route_type, self.data)\n'
            '        # Woow P1: output token limits must be integers 1..2**31-1, before any provider call.\n'
            '        from litellm.proxy.woow_request_guard import validate_token_limits\n'
            '\n'
            '        validate_token_limits(route_type, self.data)\n'
            '        self.data = await add_litellm_data_to_request(\n',
            1,
        ),),
    ),
    'proxy/spend_tracking/budget_reservation.py': (
        '433bf0a8f2e4ed5d7301c49c4f7d7151da65ed509157429b85bb32f53869cca5',
        'b88a0cd59236fc94c3ce5a389b387fbdbb0f302153378ee132b9a70a5e58a31f',
        (
            (
                'from litellm.proxy.utils import PrismaClient, ProxyLogging\n',
                'from litellm.proxy.utils import PrismaClient, ProxyLogging\n'
                'from litellm.proxy import woow_request_guard as _woow_guard  # Woow P1 budget admission\n',
                1,
            ),
            (
                '    if fail_closed_budget_enforcement and current_spend - counter.max_budget > 1e-12:\n',
                '    # Woow P1: strict admission (default) refuses an estimate that does not fit the remaining\n'
                '    # budget instead of shrinking the reservation; WOOW_BUDGET_ADMISSION=resize restores upstream.\n'
                '    strict: Final = fail_closed_budget_enforcement or _woow_guard.budget_admission_mode() == "strict"\n'
                '    if strict and current_spend - counter.max_budget > 1e-12:\n',
                1,
            ),
            (
                '            f"{counter.entity_type}={counter.entity_id} "\n',
                '            f"{counter.entity_type} budget. "  # Woow P1: never echo the key hash or entity id\n',
                1,
            ),
            (
                '    if reservation_cost is None or reservation_cost <= 0:\n'
                '        return None\n',
                '    if reservation_cost is None:\n'
                '        # Woow P1: a budget-limited key on a model without a price is logged (or refused).\n'
                '        _woow_guard.check_unpriced_budgeted_request(get_model_from_request(request_body, route, llm_router=llm_router))\n'
                '    if reservation_cost is None or reservation_cost <= 0:\n'
                '        return None\n',
                1,
            ),
            (
                '        if model_group_info is not None:\n'
                '            return model_group_info.model_dump()\n',
                '        if model_group_info is not None:\n'
                '            # Woow P1: per-model budget reserves (model_info.woow_*) live on the deployments.\n'
                '            return _woow_guard.with_deployment_overrides(\n'
                '                model_group_info.model_dump(), llm_router.get_model_list(model_name=model) or []\n'
                '            )\n',
                1,
            ),
            (
                '    if counted is not None:\n'
                '        return counted\n',
                '    if counted is not None:\n'
                '        # Woow P1: reasoning models get an input floor for the provider\'s hidden prompt.\n'
                '        return _woow_guard.reserve_input_tokens(model_info, counted)\n',
                1,
            ),
            (
                '    return min(DEFAULT_MAX_OUTPUT_TOKENS_FALLBACK if requested is None else requested, model_ceiling)\n',
                '    # Woow P1: client max_tokens does not bound hidden reasoning; reserve at least R tokens.\n'
                '    return _woow_guard.reserve_output_tokens(\n'
                '        request_body,\n'
                '        model_info,\n'
                '        min(DEFAULT_MAX_OUTPUT_TOKENS_FALLBACK if requested is None else requested, model_ceiling),\n'
                '        model_ceiling,\n'
                '    )\n',
                1,
            ),
            (
                '    return next((tokens for tokens in map(_to_int, candidates) if tokens is not None), None)\n',
                '    # Woow P1: bool, 0 and negative values fall back to the default estimate instead of shrinking it.\n'
                '    return next((tokens for tokens in map(_woow_guard.positive_token_count, candidates) if tokens is not None), None)\n',
                1,
            ),
        ),
    ),
    'proxy/auth/auth_exception_handler.py': (
        '9da190e8c1f5db0287f420c20e875069eae16b6af76b8188378600e7347998a2',
        'ef8f940a07dcd14a044f736abaa0d5ef34cc3f56c30f903a82251fa671dc3d66',
        ((
            '            type=ProxyErrorTypes.budget_exceeded,\n'
            '            param=None,\n'
            '            code=getattr(e, "status_code", status.HTTP_429_TOO_MANY_REQUESTS),\n'
            '        )\n',
            '            type=ProxyErrorTypes.budget_exceeded,\n'
            '            param=None,\n'
            '            code=getattr(e, "status_code", status.HTTP_429_TOO_MANY_REQUESTS),\n'
            '            headers={"x-should-retry": "false"},  # Woow P1: retrying cannot succeed\n'
            '        )\n',
            1,
        ),),
    ),
}


def apply(root):
    root = Path(root)
    pending = []
    for name, (expected, result_hash, edits) in PATCHES.items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit('Refusing missing or symlink target: ' + name)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise SystemExit('Refusing changed upstream source: ' + name)
        source = raw.decode('utf-8')
        for old, new, count in edits:
            if source.count(old) != count:
                raise SystemExit('Refusing changed patch context: ' + name)
            source = source.replace(old, new)
        result = source.encode('utf-8')
        if hashlib.sha256(result).hexdigest() != result_hash:
            raise SystemExit('Refusing unexpected patched source: ' + name)
        compile(source, str(path), 'exec')
        pending.append((path, result))
    for name, (source_name, helper_hash) in HELPERS.items():
        helper_path = root / name
        if helper_path.exists() or helper_path.is_symlink():
            raise SystemExit('Refusing existing helper: ' + name)
        source = Path(__file__).with_name(source_name)
        if source.is_symlink() or not source.is_file():
            raise SystemExit('Refusing missing or symlink helper source: ' + source_name)
        helper = source.read_bytes()
        if hashlib.sha256(helper).hexdigest() != helper_hash:
            raise SystemExit('Refusing unexpected helper source: ' + name)
        compile(helper.decode('utf-8'), str(helper_path), 'exec')
        pending.append((helper_path, helper))
    for path, result in pending:
        path.write_bytes(result)
        print(path.relative_to(root).as_posix() + ': ' + hashlib.sha256(result).hexdigest())


if __name__ == '__main__':
    if len(sys.argv) > 2:
        raise SystemExit('Usage: apply_upstream_guard.py [litellm-package-root]')
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(
        next(iter(importlib.util.find_spec('litellm').submodule_search_locations))
    )
    apply(root)
