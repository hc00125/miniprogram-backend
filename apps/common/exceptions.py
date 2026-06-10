from rest_framework.views import exception_handler


def compat_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return response
    if isinstance(response.data, list):
        response.data = {'detail': response.data}
    elif isinstance(response.data, dict) and 'detail' not in response.data:
        response.data = {'detail': response.data}
    return response
