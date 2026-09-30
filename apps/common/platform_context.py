from contextvars import ContextVar
from functools import wraps

from .purchase_guard import request_client_platform


_current_client_platform = ContextVar('current_client_platform', default='other')


def current_client_platform():
    return _current_client_platform.get() or 'other'


def capture_client_platform(view):
    """Keep the current mini-program platform available to synchronous signals.

    Payment completion currently happens inside the request that queries/confirms
    the virtual payment.  Earnings signals run in the same context, so a
    ContextVar lets them apply the correct channel-cost protection without
    changing the public payment API contract.
    """

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        token = _current_client_platform.set(request_client_platform(request))
        try:
            return view(request, *args, **kwargs)
        finally:
            _current_client_platform.reset(token)

    return wrapped
