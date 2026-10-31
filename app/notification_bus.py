"""SQLite 持久事件总线及三通道通知模拟器；只入本地队列，不连接真实供应商。

生产替换点：事务 outbox/消息中间件、短信与 App Push Adapter、守护授权服务。
事件/通知唯一键保证模拟入队幂等；审计先写后标记，崩溃恢复可能补写重复审计，
不声称端到端 exactly-once，不自动重试任何银行副作用工具。
"""
import json
import uuid
from datetime import datetime, timezone


def _opaque_id(prefix):
    # 保留 UUID 全部熵，以 a-p 编码，避免数字串被敏感字段脱敏破坏证据关联。
    return prefix + uuid.uuid4().hex.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))


class NotificationBus:
    CHANNELS = {'app_push', 'sms', 'guardian'}
    RISK_EVENT_TYPES = {'transaction.deferred', 'fraud.blocked', 'auth.failed', 'device.risk'}
    EVENT_TYPES = RISK_EVENT_TYPES | {'customer_service.requested'}

    def __init__(self, audit, sanitizer, guardian_target):
        self.audit, self.db, self.lock = audit, audit.db, audit.lock
        self.sanitize, self.guardian_target = sanitizer, guardian_target
        with self.lock:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS notification_subscriptions (
                    subscription_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    channel TEXT NOT NULL, target TEXT NOT NULL, event_types_json TEXT NOT NULL,
                    UNIQUE(user_id, channel, target));
                CREATE TABLE IF NOT EXISTS notification_events (
                    event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, trace_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, event_key TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, published_audited INTEGER NOT NULL DEFAULT 0,
                    complete INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id, event_key));
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL, user_id TEXT NOT NULL, trace_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, channel TEXT NOT NULL, target TEXT NOT NULL,
                    status TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    audited INTEGER NOT NULL DEFAULT 0, UNIQUE(event_id, subscription_id));
            ''')
            self.recover_pending()

    def _guardian_targets(self, user_id):
        targets = self.guardian_target(user_id)
        return [targets] if isinstance(targets, str) else list(targets or [])

    def subscribe(self, user_id, channel, target, event_types=None):
        types = sorted((self.RISK_EVENT_TYPES if channel == 'guardian' else self.EVENT_TYPES) if event_types is None else set(event_types))
        if not user_id or not target or channel not in self.CHANNELS or not types or not set(types) <= self.EVENT_TYPES:
            raise ValueError('Invalid notification subscription')
        if channel == 'guardian' and (target not in self._guardian_targets(user_id) or not set(types) <= self.RISK_EVENT_TYPES):
            raise ValueError('Guardian authorization missing or expired')
        with self.lock:
            self.db.execute('''INSERT INTO notification_subscriptions VALUES (?,?,?,?,?)
                ON CONFLICT(user_id,channel,target) DO UPDATE SET event_types_json=excluded.event_types_json''',
                (_opaque_id('sub_'), user_id, channel, target, json.dumps(types)))
            self.db.commit()

    def _defaults(self, user_id):
        existing = {r['channel'] for r in self.list_subscriptions(user_id)}
        for channel in ('app_push', 'sms'):
            if channel not in existing or any(s['channel'] == channel and s['target'] == user_id + ':' + channel for s in self.list_subscriptions(user_id)):
                self.subscribe(user_id, channel, user_id + ':' + channel)
        for guardian in self._guardian_targets(user_id):
            self.subscribe(user_id, 'guardian', guardian)

    @staticmethod
    def _decode(row):
        result = dict(row)
        for key in ('payload_json', 'event_types_json'):
            if key in result:
                result[key.removesuffix('_json')] = json.loads(result.pop(key))
        for key in ('published_audited', 'complete', 'audited'):
            result.pop(key, None)
        return result

    def publish(self, event_type, user_id, trace_id, payload, event_key):
        if event_type not in self.EVENT_TYPES or not user_id or not trace_id or not event_key:
            raise ValueError('Invalid notification event')
        clean_payload = self.sanitize(payload)
        with self.lock:
            self._defaults(user_id)
            self.db.execute('''INSERT OR IGNORE INTO notification_events
                (event_id,user_id,trace_id,event_type,event_key,payload_json,created_at) VALUES(?,?,?,?,?,?,?)''',
                (_opaque_id('event_'), user_id, trace_id, event_type, event_key,
                 json.dumps(clean_payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
            self.db.commit()
            row = self.db.execute('SELECT * FROM notification_events WHERE user_id=? AND event_key=?', (user_id, event_key)).fetchone()
            if row['event_type'] != event_type or row['trace_id'] != trace_id:
                raise ValueError('Event key cannot be reused for another event')
            self._dispatch(row)
            return self._decode(row)

    def _dispatch(self, row):
        event = self._decode(row)
        context = event['payload']
        if not row['published_audited']:
            self.audit.write(row['trace_id'], 'event.published', dict(context, event_id=row['event_id'], event_type=row['event_type']),
                             actor=row['user_id'], risk_level=context.get('risk_level', ''), intent=context.get('operation', ''))
            self.db.execute('UPDATE notification_events SET published_audited=1 WHERE event_id=?', (row['event_id'],))
            self.db.commit()
        if row['complete']:
            return
        for sub in self.list_subscriptions(row['user_id']):
            if row['event_type'] not in sub['event_types'] or (sub['channel'] == 'guardian' and row['event_type'] not in self.RISK_EVENT_TYPES):
                continue
            allowed = sub['channel'] != 'guardian' or sub['target'] in self._guardian_targets(row['user_id'])
            status = 'LOCAL_QUEUED' if allowed else 'SUPPRESSED'
            self.db.execute('''INSERT OR IGNORE INTO notifications
                (notification_id,event_id,subscription_id,user_id,trace_id,event_type,channel,target,status,payload_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (_opaque_id('notice_'), row['event_id'], sub['subscription_id'], row['user_id'], row['trace_id'],
                 row['event_type'], sub['channel'], sub['target'], status, row['payload_json'], row['created_at']))
            self.db.commit()
            notice = self.db.execute('SELECT * FROM notifications WHERE event_id=? AND subscription_id=?',
                                     (row['event_id'], sub['subscription_id'])).fetchone()
            if not notice['audited']:
                # 崩溃留下的未审计回执尚未公开；恢复时必须再次执行授权检查。
                if not allowed and notice['status'] != 'SUPPRESSED':
                    self.db.execute("UPDATE notifications SET status='SUPPRESSED' WHERE notification_id=?", (notice['notification_id'],))
                    self.db.commit()
                    notice = dict(notice, status='SUPPRESSED')
                self.audit.write(row['trace_id'], 'notification.queued' if notice['status'] == 'LOCAL_QUEUED' else 'notification.suppressed',
                                 dict(context, notification_id=notice['notification_id'], event_id=row['event_id'],
                                      channel=sub['channel'], target=sub['target'], status=notice['status']),
                                 actor=row['user_id'], risk_level=context.get('risk_level', ''), intent=context.get('operation', ''))
                self.db.execute('UPDATE notifications SET audited=1 WHERE notification_id=?', (notice['notification_id'],))
                self.db.commit()
        self.db.execute('UPDATE notification_events SET complete=1 WHERE event_id=?', (row['event_id'],))
        self.db.commit()

    def recover_pending(self):
        with self.lock:
            for row in self.db.execute('SELECT * FROM notification_events WHERE complete=0 ORDER BY created_at').fetchall():
                self._dispatch(row)

    def list_subscriptions(self, user_id):
        with self.lock:
            return [self._decode(r) for r in self.db.execute('SELECT * FROM notification_subscriptions WHERE user_id=? ORDER BY rowid', (user_id,))]

    def list_events(self, user_id, trace_id=''):
        with self.lock:
            return [self._decode(r) for r in self.db.execute(
                'SELECT * FROM notification_events WHERE user_id=? AND (?="" OR trace_id=?) ORDER BY rowid',
                (user_id, trace_id, trace_id))]

    def list_notifications(self, user_id, trace_id='', channel=''):
        if channel and channel not in self.CHANNELS:
            raise ValueError('Unknown notification channel')
        with self.lock:
            return [self._decode(r) for r in self.db.execute(
                'SELECT * FROM notifications WHERE user_id=? AND (?="" OR trace_id=?) AND (?="" OR channel=?) AND audited=1 ORDER BY rowid',
                (user_id, trace_id, trace_id, channel, channel))]
