"""Version 1 event contract shared with the orders owner. No business pricing."""
import json
import uuid
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_aware

EVENT_TYPES = frozenset({'public_slots.opened','public_slots.changed','public_slots.closed','designation.created','designation.closed','order.cancelled','replacement.opened','replacement.resolved'})
PUBLIC_FIELDS = frozenset({'published_at','participant_count','player_type_label','boss_note','status','slot_summary'})
# Designated display is provided by the audited existing template, not calculated here.
DESIGNATION_FIELDS = PUBLIC_FIELDS | frozenset({'package_label','spec_label','duration_label','price_label','deadline_label'})
ENVELOPE_FIELDS = frozenset({'schema_version','event_id','event_type','source_key','order_id','designation_id','revision','occurred_at','expires_at','fulfillment_mode','paid','audience','safe_snapshot','public_epoch'})

def validate_envelope(value):
    if not isinstance(value,dict) or set(value)-ENVELOPE_FIELDS:
        raise ValueError('Unknown event fields')
    required=ENVELOPE_FIELDS-{'public_epoch','designation_id'}
    if required-set(value):
        raise ValueError('Missing event fields')
    data=json.loads(json.dumps(value,allow_nan=False))
    if data['schema_version']!=1 or data['event_type'] not in EVENT_TYPES:
        raise ValueError('Unsupported schema/type')
    uuid.UUID(str(data['event_id']))
    for field in ['order_id','revision']:
        if type(data[field]) is not int or data[field]<1:
            raise ValueError('Invalid integer')
    if data.get('designation_id') is not None and (type(data['designation_id']) is not int or data['designation_id']<1):
        raise ValueError('Invalid designation')
    if type(data.get('public_epoch',0)) is not int or data.get('public_epoch',0)<0:
        raise ValueError('Invalid epoch')
    if not isinstance(data['source_key'],str) or not 1<=len(data['source_key'])<=200:
        raise ValueError('Invalid source key')
    for field in ['occurred_at','expires_at']:
        parsed=parse_datetime(data[field])
        if parsed is None or not is_aware(parsed):
            raise ValueError('Aware ISO datetime required')
    if data['fulfillment_mode'] not in {'public','targeted'} or type(data['paid']) is not bool:
        raise ValueError('Invalid fulfillment facts')
    if data['audience'] not in {'public_channel','designated_dm'}:
        raise ValueError('Invalid audience')
    if data['audience']=='public_channel' and (data['fulfillment_mode']!='public' or data.get('designation_id')):
        raise ValueError('Private event cannot become public')
    if data['audience']=='designated_dm' and not data.get('designation_id'):
        raise ValueError('Designation ID required')
    if data['fulfillment_mode']=='targeted' and not data['paid']:
        raise ValueError('Unpaid targeted notification forbidden')
    allowed=PUBLIC_FIELDS if data['audience']=='public_channel' else DESIGNATION_FIELDS
    snap=data['safe_snapshot']
    if not isinstance(snap,dict) or set(snap)-allowed:
        raise ValueError('Unsafe snapshot fields')
    for key,item in snap.items():
        if key=='slot_summary':
            if not isinstance(item,list) or len(item)>20 or any(not isinstance(x,str) or len(x)>200 for x in item):
                raise ValueError('slot_summary must be short display labels')
        elif key=='participant_count':
            if type(item) is not int or item<0 or item>1000:
                raise ValueError('Invalid participant count')
        elif not isinstance(item,str) or len(item)>2000:
            raise ValueError('Invalid display value')
    return data
