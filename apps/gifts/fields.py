from django.core.exceptions import ValidationError
from django.db import models


class DiamondPriceField(models.PositiveIntegerField):
    """Do not silently truncate fractional diamonds or accept booleans."""
    def to_python(self, value):
        if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and value.isdecimal()):
            raise ValidationError('钻石单价必须为正整数')
        return super().to_python(value)
