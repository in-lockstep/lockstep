"""Certificate verification, and the one declared way to turn it off.

This runtime holds a profile's credentials and talks to whatever host a pipeline names. It used to
disable certificate verification unconditionally — extracted from a harness pointed at one staging
environment behind a self-signed certificate, where it was a local convenience, and shipped as a
permanent default for everybody.

These tests exist so that never silently comes back: the default is verification, and turning it off
requires a profile to say so.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from pipeline_exec.config import PROFILE_KEYS, ExecConfig
from pipeline_exec.executors.api_session import ApiSession

SOURCE = Path(__file__).parent.parent / "src/pipeline_exec/executors/api_session.py"


def session(**kwargs):
    return ApiSession("https://api.example.test", "u", "p", **kwargs)


# --- the default ------------------------------------------------------------


def test_certificates_are_verified_by_default():
    assert session()._verify is True


def test_nothing_in_the_module_hardcodes_an_unverified_client():
    """The regression this file exists for: a client constructed with verification off."""
    source = SOURCE.read_text(encoding="utf-8")
    calls = [line for line in source.splitlines() if "AsyncClient(" in line]
    assert calls, "expected the module to construct clients"
    for line in calls:
        assert "verify=self._verify" in line, line


def test_every_request_path_uses_the_same_decision():
    """Login and request are separate clients; one of them verifying is not a policy."""
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count("verify=self._verify") == source.count("AsyncClient(")


# --- the declared opt-out ---------------------------------------------------


def test_a_profile_may_turn_verification_off():
    context = session(insecure_tls=True)._verify
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is False
    assert context.verify_mode is ssl.CERT_NONE


def test_turning_it_off_is_announced(caplog):
    """A silent downgrade is the thing that made the original default survive so long."""
    with caplog.at_level("WARNING"):
        session(insecure_tls=True)
    assert "TLS verification is OFF" in caplog.text
    assert "api.example.test" in caplog.text


def test_leaving_it_on_says_nothing():
    assert session()._verify is True


# --- how a pipeline declares it ---------------------------------------------


def test_the_profile_key_exists_so_a_spec_can_declare_it():
    assert "insecure_tls" in PROFILE_KEYS


def test_an_undeclared_profile_verifies():
    assert ExecConfig().profile_insecure_tls == ""


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", " true "])
def test_the_values_somebody_would_actually_write(raw):
    assert raw.strip().lower() in ("1", "true", "yes")


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "maybe"])
def test_anything_else_keeps_verification_on(raw):
    assert raw.strip().lower() not in ("1", "true", "yes")
