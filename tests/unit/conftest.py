"""Shared fixtures for unit tests.

The provider layer keeps process-wide caches -- provider instances, the Simyan
client, the shared rate limiters -- which are exactly what makes a library sweep
affordable, and exactly what leaks state between tests. Reset them per test.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_provider_instance_cache():
    from models.providers import invalidate_provider_cache
    invalidate_provider_cache()
    yield
    invalidate_provider_cache()


@pytest.fixture(autouse=True)
def _reset_provider_rate_limiters():
    from helpers.rate_limit import reset_all_limiters
    reset_all_limiters()
    yield
    reset_all_limiters()


@pytest.fixture(autouse=True)
def _reset_cv_client_cache():
    from models.comicvine import invalidate_cv_client_cache
    invalidate_cv_client_cache()
    yield
    invalidate_cv_client_cache()
