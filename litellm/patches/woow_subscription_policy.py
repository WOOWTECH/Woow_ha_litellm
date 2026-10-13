"""HA runtime deployment policy; no plugin import, routes, or persistent writes."""
from pathlib import Path

POLICY_PATH = Path('/run/woow/chatgpt-subscription')


def deployment_enabled(params):
    # Both DB loading and Router config loading can contain env references.
    from litellm import get_secret

    def resolved(key):
        value = params.get(key)
        if isinstance(value, str) and value.startswith('os.environ/'):
            value = get_secret(value)
        return value

    model = resolved('model')
    provider = resolved('custom_llm_provider')
    if provider != 'chatgpt' and not (isinstance(model, str) and model.startswith('chatgpt/')):
        return True
    # init-woow-env writes this root-owned tmpfs file before LiteLLM starts.
    # Missing/unreadable/invalid policy fails closed for subscription deployments.
    try:
        return POLICY_PATH.read_text() == 'enabled\n'
    except (OSError, UnicodeError):
        return False
