"""
Read-through cache for the public catalogue endpoints.

Catalogue data is read far more often than it is written, so list/detail
responses are cached. Instead of tracking every key that might be affected
by a write, all keys embed a *version* token; any write to a catalogue model
bumps the version (see `signals.py`), which invalidates everything at once.

The cache is an optimisation only: if Redis is unavailable the API keeps
serving from the database.
"""

import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from rest_framework.response import Response

logger = logging.getLogger(__name__)

VERSION_KEY = "catalog:version"


def _current_version() -> str:
    version = cache.get(VERSION_KEY)
    if version is None:
        cache.add(VERSION_KEY, uuid.uuid4().hex, timeout=None)
        version = cache.get(VERSION_KEY)
    return version


def invalidate_catalog_cache() -> None:
    try:
        cache.set(VERSION_KEY, uuid.uuid4().hex, timeout=None)
    except Exception:
        logger.warning("catalog_cache_invalidate_failed", exc_info=True)


class CatalogCacheMixin:
    """Caches successful `list` / `retrieve` responses of a ViewSet."""

    def list(self, request, *args, **kwargs):
        return self._cached(request, super().list, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        return self._cached(request, super().retrieve, *args, **kwargs)

    def _cached(self, request, handler, *args, **kwargs):
        audience = "staff" if request.user.is_staff else "public"
        key = None
        try:
            key = f"catalog:{_current_version()}:{audience}:{request.get_full_path()}"
            data = cache.get(key)
        except Exception:
            logger.warning("catalog_cache_read_failed", exc_info=True)
            data = None
        if data is not None:
            return Response(data, headers={"X-Cache": "HIT"})

        response = handler(request, *args, **kwargs)
        if response.status_code == 200 and key is not None:
            try:
                cache.set(key, response.data, timeout=settings.CATALOG_CACHE_TTL)
            except Exception:
                logger.warning("catalog_cache_write_failed", exc_info=True)
        response["X-Cache"] = "MISS"
        return response
