"""Offline only: use a pristine/patched LiteLLM 1.104.0 package on PYTHONPATH."""
import os
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['LITELLM_TELEMETRY'] = 'False'
os.environ['DO_NOT_TRACK'] = '1'
import socket
import pytest

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline contract test attempted a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
