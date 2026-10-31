"""T06 local, ephemeral transfer clarification; never an authorization store."""
from contextlib import contextmanager
from dataclasses import dataclass, field
import re
import threading
import time


@dataclass(frozen=True)
class TransferDraft:
    beneficiary: str | None
    amount_cents: int | None
    started_at: float
    turns: int


@dataclass
class _Session:
    lock: object = field(default_factory=threading.Lock)
    active: int = 0
    draft: TransferDraft | None = None


class _Turn:
    def __init__(self, store, draft, retain=True):
        self.store, self.previous, self.next_draft = store, None, None
        self.retain = retain
        self.draft = draft if draft and store.clock() - draft.started_at < store.ttl_seconds else None

    def resolve(self, text):
        """Only exact amount/known-recipient fragments can inherit missing slots."""
        if self.draft is None:
            return text
        amount = re.fullmatch(r'(?:金额(?:是|为|：|:)?)?\s*([+\-]?\d[\d,]*(?:\.\d+)?)\s*(?:元|块|人民币)', text.strip())
        recipient = re.fullmatch(r'(?:转给|给|收款人(?:是|为|：|:)?)\s*([\u4e00-\u9fa5A-Za-z]{1,12})', text.strip())
        if not amount and not recipient:
            return text
        if recipient and recipient[1] not in self.store.recipients:
            return text
        self.previous = self.draft
        name = recipient[1] if recipient else self.draft.beneficiary
        cents = self.draft.amount_cents
        value = amount[1] if amount else (f'{cents // 100}.{cents % 100:02d}' if cents is not None else '')
        return (f'给{name}转账' if name else '转账') + (value + '元' if value else '')

    def remember(self, entities):
        # No raw dialogue, note, account number, tool facts, credentials or family authority.
        if not self.retain:
            return
        name, cents = entities.get('beneficiary'), entities.get('amount_cents')
        if (entities.get('amount_error') or entities.get('account_requires_verification')
                or entities.get('note') or entities.get('fraud_signals')
                or (name is not None and name not in self.store.recipients)
                or (cents is not None and (type(cents) is not int or cents <= 0))
                or (name is not None and cents is not None)):
            return
        turns = self.previous.turns + 1 if self.previous else 1
        if turns >= self.store.max_turns:
            return
        started = self.previous.started_at if self.previous else self.store.clock()
        self.next_draft = TransferDraft(name, cents, started, turns)


class TransferDraftStore:
    """At most 256 active session slots, fixed five-minute TTL, three messages.

    Per-key locks serialize concurrent turns. Capacity evicts an idle slot; when
    every slot is busy a new request runs without remembered context.
    """
    def __init__(self, recipients, ttl_seconds=300, max_turns=3, max_sessions=256, clock=time.monotonic):
        self.recipients = frozenset(recipients)
        self.ttl_seconds, self.max_turns, self.max_sessions = ttl_seconds, max_turns, max_sessions
        self.clock = clock
        self._lock, self._sessions = threading.Lock(), {}

    def __len__(self):
        with self._lock:
            return sum(state.draft is not None for state in self._sessions.values())

    @contextmanager
    def turn(self, key):
        with self._lock:
            for old_key, state in list(self._sessions.items()):
                if not state.active and (state.draft is None or self.clock() - state.draft.started_at >= self.ttl_seconds):
                    del self._sessions[old_key]
            state = self._sessions.get(key)
            if state is None:
                if len(self._sessions) >= self.max_sessions:
                    idle = next((k for k, s in self._sessions.items() if not s.active), None)
                    if idle is not None:
                        del self._sessions[idle]
                if len(self._sessions) < self.max_sessions:
                    state = self._sessions[key] = _Session()
            if state is not None:
                state.active += 1
        if state is None:
            yield _Turn(self, None, retain=False)
            return
        with state.lock:
            access = _Turn(self, state.draft)
            state.draft = None
            try:
                yield access
            finally:
                with self._lock:
                    state.draft = access.next_draft
                    state.active -= 1
                    if not state.active and state.draft is None:
                        self._sessions.pop(key, None)
