"""Persistent local case workflow. Never executes or settles bank transactions."""
import json
import uuid
from datetime import datetime, timezone


class HandoffConflict(RuntimeError):
    pass


class HandoffQueue:
    RESOLUTIONS = {'GUIDANCE_PROVIDED', 'REFERRED_OFFICIAL', 'NEEDS_RECONCILIATION'}

    def __init__(self, audit):
        self.audit, self.db, self.lock = audit, audit.db, audit.lock
        with self.lock:
            self.db.execute('''CREATE TABLE IF NOT EXISTS local_handoff_cases (
                case_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL, device_id TEXT NOT NULL, trace_id TEXT NOT NULL,
                status TEXT NOT NULL, operator_id TEXT NOT NULL, operator_device TEXT NOT NULL,
                resolution TEXT NOT NULL, card_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            self.db.commit()

    @staticmethod
    def _view(row, include_card=False):
        result = {k: row[k] for k in ('case_id', 'trace_id', 'status', 'resolution', 'created_at', 'updated_at')}
        result.update(mode='LOCAL_DEMO_ONLY', message='本地人工接管演示；工单结论不代表银行交易完成或已接通官方客服。')
        if include_card:
            result.update(user_id=row['user_id'], operator_id=row['operator_id'], card=json.loads(row['card_json']))
        return result

    def _record(self, row, event, actor, device):
        with self.audit.identity(actor, row['user_id'], device):
            self.audit.write(row['trace_id'], event,
                {'case_id': row['case_id'], 'status': row['status'], 'resolution': row['resolution'],
                 'decision_reason': '仅更新本地接管工单，不授权、不执行银行业务'},
                actor=actor, intent='human_handoff', commit=False)

    def enqueue(self, card, actor, device, trace, trigger):
        encoded = json.dumps(card, ensure_ascii=False)
        if not actor or not device or not trace or len(encoded.encode('utf-8')) > 32768:
            raise ValueError('Invalid handoff evidence')
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM local_handoff_cases WHERE source_key=?', (trace + ':' + trigger,)).fetchone()
            if row:
                if row['user_id'] != actor or row['device_id'] != device:
                    raise PermissionError('Handoff identity changed')
                return self._view(row)
            case_id = 'case_' + uuid.uuid4().hex.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute('INSERT INTO local_handoff_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (case_id, trace + ':' + trigger, actor, device, trace, 'LOCAL_QUEUED', '', '', '', encoded, now, now))
            row = self.db.execute('SELECT * FROM local_handoff_cases WHERE case_id=?', (case_id,)).fetchone()
            self._record(row, 'human.case.queued', actor, device)
            return self._view(row)

    def status(self, case_id, actor, device):
        with self.lock:
            row = self.db.execute('SELECT * FROM local_handoff_cases WHERE case_id=?', (case_id,)).fetchone()
            if not row or row['user_id'] != actor or row['device_id'] != device:
                raise PermissionError('Case unavailable to this identity')
            return self._view(row)

    def inbox(self, offset=0):
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError('Invalid page offset')
        with self.lock:
            rows = self.db.execute('SELECT * FROM local_handoff_cases ORDER BY created_at,case_id LIMIT 50 OFFSET ?', (offset,)).fetchall()
            return [self._view(row, True) for row in rows]

    def transition(self, case_id, action, operator, device, resolution=''):
        if action not in {'claim', 'resolve'} or not operator or not device:
            raise ValueError('Invalid local operator action')
        if action == 'resolve' and resolution not in self.RESOLUTIONS:
            raise ValueError('Only fixed local case conclusions are allowed')
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM local_handoff_cases WHERE case_id=?', (case_id,)).fetchone()
            if not row:
                raise PermissionError('Case unavailable')
            same_operator = row['operator_id'] == operator and row['operator_device'] == device
            if action == 'claim':
                if row['status'] == 'LOCAL_CLAIMED' and same_operator:
                    return self._view(row)
                if row['status'] != 'LOCAL_QUEUED':
                    raise HandoffConflict('Case cannot be claimed')
                state, conclusion, event = 'LOCAL_CLAIMED', '', 'human.case.claimed'
            else:
                if row['status'] == 'LOCAL_CLOSED' and same_operator and row['resolution'] == resolution:
                    return self._view(row)
                if row['status'] != 'LOCAL_CLAIMED' or not same_operator:
                    raise HandoffConflict('Case must be resolved by its assigned operator and device')
                trigger = json.loads(row['card_json']).get('redacted_context', {}).get('trigger')
                if trigger == 'unknown_result' and resolution != 'NEEDS_RECONCILIATION':
                    raise ValueError('Unknown bank result still requires reconciliation')
                state, conclusion, event = 'LOCAL_CLOSED', resolution, 'human.case.closed'
            self.db.execute('''UPDATE local_handoff_cases SET status=?,operator_id=?,operator_device=?,resolution=?,updated_at=?
                WHERE case_id=?''', (state, operator, device, conclusion, datetime.now(timezone.utc).isoformat(), case_id))
            updated = self.db.execute('SELECT * FROM local_handoff_cases WHERE case_id=?', (case_id,)).fetchone()
            self._record(updated, event, operator, device)
            return self._view(updated)
