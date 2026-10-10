"""Export synthetic, real wall JSON for the frontend replay; no production I/O.

Run from repository root with installed project requirements:
python -m apps.patronage.tests.export_wall_fixture /tmp/wall-fixture.json
"""
import json
import os
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit('Usage: python -m apps.patronage.tests.export_wall_fixture OUTPUT.json')
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_patronage_wall_test'
import django
django.setup()
from django.test.runner import DiscoverRunner
from django.utils import timezone
from .test_wall import PatronageWallTests

runner = DiscoverRunner(verbosity=0, interactive=False)
runner.setup_test_environment()
database = runner.setup_databases()
try:
    fixture = PatronageWallTests('test_empty_wall_is_a_complete_contract_with_no_pagination')
    fixture.setUp()
    fixture.now = timezone.now()
    first = fixture.player(user=fixture.user(), total_rating=10, rating_count=2)
    second = fixture.player(total_rating=4, rating_count=1)
    boss = fixture.user(nickname='offline-example-boss')
    fixture.grant(first, boss=boss)
    fixture.grant(first, source='day_pass_bonus')
    fixture.grant(second, boss=boss)
    response = fixture.read()
    assert response.status_code == 200
    payload = {'statusCode': response.status_code, 'data': response.json()}
    assert payload['data']['count'] == 2
    Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    print('Exported actual offline Django response: 2 players, 3 crowns; no live network.')
finally:
    runner.teardown_databases(database)
    runner.teardown_test_environment()
