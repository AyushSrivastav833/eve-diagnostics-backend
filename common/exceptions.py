"""
Domain exceptions and a single, consistent error envelope for the API.

Every error response has the shape::

    {"error": {"code": "booking_not_payable", "message": "...", "details": {...}}}

so clients can branch on a stable machine-readable `code`.
"""

import logging

from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class DomainError(APIException):
    """Base class for business-rule violations raised from the service layer."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "domain_error"
    default_detail = "The request violates a business rule."

    def __init__(self, detail=None, code=None, details=None):
        super().__init__(detail=detail, code=code)
        self.code = code or self.default_code
        self.extra = details or {}


class ConflictError(DomainError):
    """The request is valid but conflicts with the current state of a resource."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "The resource is not in a state that allows this operation."


class UnprocessableError(DomainError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "unprocessable"
    default_detail = "The request could not be processed."


def _first_code(codes):
    if isinstance(codes, str):
        return codes
    if isinstance(codes, list) and codes:
        return _first_code(codes[0])
    if isinstance(codes, dict) and codes:
        return _first_code(next(iter(codes.values())))
    return "error"


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        # Unhandled exception -> Django returns a 500; make sure it is logged with context.
        logger.exception("unhandled_exception", extra={"view": context.get("view").__class__.__name__})
        return None

    if isinstance(exc, ValidationError):
        body = {
            "code": "validation_error",
            "message": "Invalid request. See `details` for the offending fields.",
            "details": response.data,
        }
    elif isinstance(exc, DomainError):
        body = {"code": exc.code, "message": str(exc.detail), "details": exc.extra}
    else:
        detail = response.data.get("detail", response.data) if isinstance(response.data, dict) else response.data
        code = _first_code(exc.get_codes()) if isinstance(exc, APIException) else "error"
        body = {"code": code, "message": str(detail), "details": {}}

    response.data = {"error": body}
    return response
