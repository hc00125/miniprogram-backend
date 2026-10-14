from django.test import SimpleTestCase
from django.db import models, ProgrammingError
from apps.common.player_model_version import archive_filter, is_archived


class LegacyCompatibilityPlayer(models.Model):
    class Meta:
        app_label = 'compatibility_probe'
        managed = False


class FutureCompatibilityPlayer(models.Model):
    is_archived = models.BooleanField(default=False)
    class Meta:
        app_label = 'compatibility_probe'
        managed = False


class ModelVersionTests(SimpleTestCase):
    def test_old_model_has_no_archive_filter(self):
        self.assertEqual(archive_filter(LegacyCompatibilityPlayer), {})
        self.assertFalse(is_archived(LegacyCompatibilityPlayer()))

    def test_new_model_preserves_archive_semantics(self):
        self.assertEqual(archive_filter(FutureCompatibilityPlayer), {'is_archived': False})
        self.assertTrue(is_archived(FutureCompatibilityPlayer(is_archived=True)))
        self.assertFalse(is_archived(FutureCompatibilityPlayer(is_archived=False)))

    def test_declared_field_missing_schema_error_is_not_swallowed(self):
        obj = FutureCompatibilityPlayer()
        del obj.__dict__['is_archived']
        def missing_column(*args, **kwargs):
            raise ProgrammingError('column is_archived does not exist')
        obj.refresh_from_db = missing_column
        with self.assertRaisesRegex(ProgrammingError, 'is_archived'):
            is_archived(obj)
