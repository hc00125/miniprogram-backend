from functools import wraps
from django.core.exceptions import ValidationError as ModelValidationError
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from apps.accounts.authentication import LenientJWTAuthentication


class StrictInteger(serializers.IntegerField):
    def to_internal_value(self, value):
        if type(value) is not int:
            raise ValidationError('必须使用JSON整数')
        return super().to_internal_value(value)


def errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValidationError, ModelValidationError) as exc:
            detail = getattr(exc, 'detail', None)
            code = str(detail.get('code', 'INVALID_REQUEST')) if isinstance(detail, dict) else 'INVALID_REQUEST'
            if not isinstance(detail, dict) and 'CONFIG_CHANGED' in str(exc):
                code = 'CONFIG_CHANGED'
            status = 409 if code in ('IDEMPOTENCY_CONFLICT', 'PRICE_CHANGED', 'CONFIG_CHANGED', 'PAYMENT_PENDING', 'ORDER_NOT_ELIGIBLE') else 403 if code in ('POLICY_UNCONFIRMED', 'FEATURE_DISABLED', 'RECIPIENT_INELIGIBLE', 'ACCOUNT_RESTRICTED') else 400
            return Response({'code': code, 'detail': str(exc)}, status=status)
    return wrapped


def endpoint(methods):
    def decorate(fn):
        return api_view(methods)(authentication_classes([LenientJWTAuthentication])(
            permission_classes([IsAuthenticated])(errors(fn))))
    return decorate


def validated(cls, request):
    extra = set(request.data) - set(cls().fields)
    if extra:
        raise ValidationError({'code': 'INVALID_REQUEST', 'detail': '不接受额外字段'})
    serializer = cls(data=request.data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


