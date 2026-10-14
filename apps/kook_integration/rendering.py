import json

# All user-controlled text stays plain-text, including notes. Never delete
# phone-like content from the boss's manually entered requirement.
def plain(value, limit=500):
    return str(value).replace('@','＠').replace('(met)','（met）').replace('(rol)','（rol）').replace('(chn)','（chn）')[:limit]

def render(event, mention_all=False, url_link=""):
    from .navigation import safe_url_link
    url_link = url_link if safe_url_link(url_link) else ""
    if event.event_type=='test.dm':
        lines=['测试通知','KOOK通知绑定成功。打开小程序 → 接单大厅/待确认邀请。']
    else:
        snapshot=event.payload['safe_snapshot']
        private = event.payload['audience']=='designated_dm'
        targeted = private and event.payload.get('fulfillment_mode') == 'targeted'
        labels=[('published_at','发布时间'),('player_type_label','陪玩类型'),('boss_note','老板备注')]
        if private:
            labels += [('package_label','商品'),('duration_label','预订时长'),('price_label','订单钻石'),('deadline_label','邀请截止')]
        lines=[label+'：'+plain(snapshot.get(key, '无') or '无') for key,label in labels]
        lines.insert(1, '服务对象：仅本人' if targeted else '人数：'+plain('、'.join(snapshot.get('slot_summary', [])) or str(snapshot.get('participant_count', 0))))
        if private:
            lines.append('订单类型：'+('专属服务' if targeted else '公开组局'))
        terminal = event.payload['audience'] == 'public_channel' and event.event_type in {
            'public_slots.closed', 'replacement.resolved', 'order.cancelled'}
        if terminal:
            # Only audited, frozen close labels; never display an active/raw
            # execution status from a legacy terminal payload or query live ORM.
            status = snapshot.get('status')
            if event.event_type == 'order.cancelled':
                status = '已取消 · 不可接单'
            elif status not in {'已接满 · 不可接单', '补位已结束 · 不可接单', '已关闭 · 不可接单'}:
                status = '已关闭 · 不可接单'
            lines.append('状态：'+status)
        elif not private and event.event_type in {'public_slots.opened', 'public_slots.changed', 'replacement.opened'}:
            lines.append('状态：可接单')
        elif snapshot.get('status'):
            lines.append('状态：'+plain(snapshot['status']))
        lines.append(('' if url_link else '通知链接暂不可用，请手动')+'打开小程序 → 接单大厅/待确认邀请；能否接单以小程序实时状态为准。')
    modules=[{'type':'section','text':{'type':'plain-text','content':line}} for line in lines]
    if url_link:
        modules.append({'type': 'action-group', 'elements': [{'type': 'button', 'theme': 'primary',
            'text': {'type': 'plain-text', 'content': '打开小程序查看'}, 'click': 'link', 'value': url_link}]})
    if mention_all:
        # Only caller-controlled production initial public create can set this.
        modules.insert(0,{'type':'section','text':{'type':'kmarkdown','content':'(met)all(met)'}})
    return json.dumps([{'type':'card','theme':'secondary','size':'lg','modules':modules}],ensure_ascii=False,separators=(',',':'))
