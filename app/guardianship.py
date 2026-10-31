"""C4 durable demo grants and conservative daily reservations.

No user-facing grant creation/update API. Configuration is a fixture for an owner's
out-of-band grant; production replaces this with verified owner consent and CAS.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from policy_config import get_policy


@dataclass(frozen=True)
class Guardianship:
    grant_id: str
    guardian_id: str
    subject_id: str
    authorization_type: str
    daily_limit_cents: int
    valid_from: str
    expires_at: str
    revoked: bool = False

    def validate(self):
        if (self.authorization_type not in {'read_only', 'limited_transfer', 'full'}
                or type(self.daily_limit_cents) is not int or self.daily_limit_cents < 0
                or type(self.revoked) is not bool or self.guardian_id == self.subject_id
                or not all((self.grant_id, self.guardian_id, self.subject_id))):
            raise ValueError('Invalid guardianship')
        start, end = datetime.fromisoformat(self.valid_from), datetime.fromisoformat(self.expires_at)
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError('Invalid guardianship validity')
        return self

    def active(self, now):
        return not self.revoked and datetime.fromisoformat(self.valid_from) <= now < datetime.fromisoformat(self.expires_at)


class GuardianshipStore:
    READ_INTENTS = {'balance_query', 'transaction_query', 'bill_analysis'}
    OWNER_ONLY = {'password_change', 'card_manage'}

    def __init__(self, path=None, config_path=None):
        config_path = config_path or Path(__file__).resolve().parents[1] / 'config/guardianship.json'
        config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.accounts = {a['user_id']: a for a in config['accounts']}
        self.small_max = get_policy()['family_small_transfer_max_cents']
        if type(self.small_max) is not int or self.small_max <= 0:
            raise ValueError('Invalid small transfer ceiling')
        if len(self.accounts) != len(config['accounts']) or len({a['demo_session'] for a in self.accounts.values()}) != len(self.accounts):
            raise ValueError('Duplicate account/session fixture')
        for account in self.accounts.values():
            if not all(isinstance(account[k], str) and account[k] for k in ('user_id', 'device_id', 'demo_session')) or type(account['is_elderly']) is not bool:
                raise ValueError('Invalid identity fixture')
            if type(account['opening_cents']) is not int or account['opening_cents'] < 0:
                raise ValueError('Invalid opening balance')
        self.lock = threading.RLock()
        self.mutation_lock = threading.RLock()
        self.clock = lambda: datetime.now(timezone.utc)
        self.db = sqlite3.connect(str(path) if path is not None else ':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS guardianships (
                    grant_id TEXT PRIMARY KEY, guardian_id TEXT NOT NULL, subject_id TEXT NOT NULL,
                    authorization_type TEXT NOT NULL, daily_limit_cents INTEGER NOT NULL,
                    valid_from TEXT NOT NULL, expires_at TEXT NOT NULL, revoked INTEGER NOT NULL,
                    UNIQUE(guardian_id,subject_id));
                CREATE TABLE IF NOT EXISTS guardianship_usage (
                    task_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, business_day TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL, status TEXT NOT NULL);
            ''')
            for data in config['guardianships']:
                item = Guardianship(**data).validate()
                if item.guardian_id not in self.accounts or item.subject_id not in self.accounts:
                    raise ValueError('Unknown guardianship account')
                self.db.execute('INSERT OR IGNORE INTO guardianships VALUES (?,?,?,?,?,?,?,?)', tuple(asdict(item).values()))

    def close(self):
        self.db.close()

    def day(self):
        return self.clock().astimezone(timezone(timedelta(hours=8))).date().isoformat()

    @staticmethod
    def decode(row):
        return Guardianship(**dict(dict(row), revoked=bool(row['revoked'])))

    def get(self, grant_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM guardianships WHERE grant_id=?', (grant_id,)).fetchone()
            return self.decode(row) if row else None

    def between(self, guardian, subject):
        with self.lock:
            row = self.db.execute('SELECT * FROM guardianships WHERE guardian_id=? AND subject_id=?',
                                  (guardian, subject)).fetchone()
            return self.decode(row) if row else None

    def update(self, grant_id, **changes):
        """Trusted fixture/admin adapter only, never exposed as a public HTTP route."""
        if not set(changes) <= {'authorization_type', 'daily_limit_cents', 'valid_from', 'expires_at', 'revoked'}:
            raise ValueError('Immutable grant identity')
        with self.mutation_lock, self.lock, self.db:
            old = self.get(grant_id)
            if old is None:
                raise ValueError('Unknown grant')
            item = replace(old, **changes).validate()
            self.db.execute('''UPDATE guardianships SET authorization_type=?,daily_limit_cents=?,
                valid_from=?,expires_at=?,revoked=? WHERE grant_id=?''',
                (item.authorization_type, item.daily_limit_cents, item.valid_from, item.expires_at, item.revoked, grant_id))
            return item

    def used(self, grant_id, excluding_task=''):
        with self.lock:
            return self.db.execute('''SELECT coalesce(sum(amount_cents),0) FROM guardianship_usage
                WHERE grant_id=? AND business_day=? AND status IN ('reserved','consumed') AND task_id<>?''',
                (grant_id, self.day(), excluding_task)).fetchone()[0]

    def grants_for(self, guardian):
        with self.lock:
            rows = self.db.execute('SELECT * FROM guardianships WHERE guardian_id=? ORDER BY grant_id', (guardian,)).fetchall()
            return [dict(asdict(self.decode(r)), remaining_cents=max(0, r['daily_limit_cents'] - self.used(r['grant_id'])),
                         active=self.decode(r).active(self.clock()), business_day=self.day()) for r in rows]

    def guardian_targets(self, subject):
        with self.lock:
            rows = self.db.execute('SELECT * FROM guardianships WHERE subject_id=? ORDER BY grant_id', (subject,)).fetchall()
            return [r['guardian_id'] for r in rows if self.decode(r).active(self.clock())]

    def evaluate(self, intent, amount, context, subject, task_id=''):
        """Pure matrix input for PolicyEngine; 'full' NEVER removes hard exclusions."""
        grant = self.get(context.get('grant_id', ''))
        mode = context.get('mode')
        reason = ''
        if (grant is None or not grant.active(self.clock()) or grant.subject_id != subject
                or context.get('subject_id') != subject or grant.guardian_id != context.get('guardian_id')):
            reason = '守护关系不存在、未生效、已过期或已撤销'
        elif mode not in {'delegate', 'assist'}:
            reason = '不支持的家庭操作方式'
        elif intent in self.READ_INTENTS:
            pass
        elif intent == 'transfer' and (type(amount) is not int or amount <= 0):
            reason = '转账金额必须为正整数分'
        elif mode == 'assist' and intent in self.OWNER_ONLY | {'transfer'}:
            pass  # owner must authenticate AND submit, checked by the runtime adapter
        elif intent in self.OWNER_ONLY:
            reason = '修改密码、挂失和解挂只能本人强认证办理，守护人只能协助发起'
        elif intent != 'transfer':
            reason = '该业务不在家庭代办白名单内'
        elif amount > self.small_max:
            reason = '大额转账不能代办，只能协助发起并由本人强认证确认'
        elif grant.authorization_type == 'read_only':
            reason = '当前只有只读查询授权'
        elif amount + self.used(grant.grant_id, task_id) > grant.daily_limit_cents:
            reason = '超过家庭授权每日可用额度'
        return {'allowed': not reason, 'reason': reason or '家庭授权有效；所有非查询操作必须被守护人本人认证',
                'owner_auth_required': intent not in self.READ_INTENTS,
                'owner_submit_required': mode == 'assist', 'grant_id': grant.grant_id if grant else ''}

    def reserve(self, context, task_id, amount, subject):
        # Caller holds the bank lock. Commit the reservation BEFORE touching core;
        # an uncertain/crashed submission remains charged to quota until reconciled.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            decision = self.evaluate('transfer', amount, context, subject, task_id)
            if not decision['allowed']:
                return False
            existing = self.db.execute('SELECT * FROM guardianship_usage WHERE task_id=?', (task_id,)).fetchone()
            if existing:
                return existing['grant_id'] == context['grant_id'] and existing['amount_cents'] == amount and existing['status'] != 'released'
            self.db.execute('INSERT INTO guardianship_usage VALUES (?,?,?,?,?)',
                            (task_id, context['grant_id'], self.day(), amount, 'reserved'))
            return True

    def settle(self, idempotency_key, status):
        parts = idempotency_key.split(':')
        if len(parts) != 3 or parts[0] != 'idem' or parts[2] != 'create_transfer':
            return
        # Reversal does not replenish daily spend (prevents quota recycling).
        state = 'consumed' if status in {'succeeded', 'reversed'} else 'released' if status == 'failed' else None
        if state:
            with self.lock, self.db:
                self.db.execute('UPDATE guardianship_usage SET status=? WHERE task_id=?', (state, parts[1]))
