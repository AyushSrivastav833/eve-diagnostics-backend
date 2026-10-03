import re
import uuid

from .logging import request_id_var

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIDMiddleware:
    """
    Attach a correlation ID to every request.

    Re-uses a well-formed incoming `X-Request-ID` (so IDs can be traced across
    services), otherwise generates one. The ID is echoed in the response and
    included in every log line emitted while handling the request.
    """

    header = "HTTP_X_REQUEST_ID"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(self.header, "")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response["X-Request-ID"] = request_id
        return response
