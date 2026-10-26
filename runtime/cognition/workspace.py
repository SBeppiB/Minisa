"""Level 0 workspace over Core's authoritative database, never model reasoning.

Existing Markdown memory and cognition.json retain their authority. SQLite owns
Phase 5 units, evidence indexes, procedures and proposal review history only.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid
import unicodedata

from core.db import Database, canonical_json

KINDS = {'goal', 'question', 'concept', 'memory', 'contradiction', 'uncertainty',
         'experience', 'decision', 'interrupted_task', 'association', 'dependency'}
PROTECTED = {'belief', 'personality', 'self_model'}
# Level 0 may durably persist one additional kind: a sourced conversation memory
# (target 'memory_consolidation'). Every other review target stays review-only.
MEMORY_NOTE_IDENTITY = 'dream:memory-note:v1:'
# Words that carry no retrieval meaning on their own. A durable memory is only
# recalled for a query that shares a meaningful word with its own sourced text.
MEMORY_STOPWORDS = frozenset('''a an the is are was were be been being to of for and or
but if in on at by with from as it its this that these those there here we i you he she
they them us our your my me our not no yes do does did done can could should would will
shall may might must just also very really about into over under again then than so
what when where which who whom how why please tell know remember say said thing things
'''.split())
CHANGE_FIELDS = {
    'unit': {'type','kind','subject','evidence'},
    'resolve': {'type','unit_id','evidence'},
    'procedure': {'type','situation','procedure','evidence'},
    'review': {'type','target','claim','evidence'},
    'association': {'type','unit_ids','evidence'},
    'memory': {'type','title','fact','evidence'},
}


@dataclass(frozen=True)
class WorkspacePolicy:
    active_limit: int = 32
    context_bytes: int = 6000
    half_life: float = 21600
    pressure_threshold: float = 12
    cooldown: float = 1800
    idle_seconds: float = 900
    calls_per_hour: int = 2
    max_attempts: int = 3
    interrupt_threshold: float = .8
    interrupt_cooldown: float = 300
    interrupt_limit: int = 4
    snapshot_evidence: int = 32
    proposal_limit: int = 12
    dream_snapshot_bytes: int = 24000
    starvation_slots: int = 1
    starvation_reserve: int = 2600

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('Invalid workspace policy: ' + name)
            if value <= 0 and name not in ('starvation_slots', 'starvation_reserve'):
                raise ValueError('Invalid workspace policy: ' + name)
        if self.starvation_slots not in (0, 1):
            raise ValueError('Invalid starvation slot count')
        if self.starvation_reserve < 0:
            raise ValueError('Invalid starvation reserve')
        for name in ('active_limit', 'context_bytes', 'calls_per_hour', 'max_attempts',
                     'interrupt_limit', 'snapshot_evidence', 'proposal_limit',
                     'starvation_slots', 'starvation_reserve'):
            if type(getattr(self, name)) is not int:
                raise ValueError('Integer budget required')


def score(row, now, policy):
    age = max(0, now - row['activated_at'])
    decay = 2 ** (-age / (policy.half_life * (1 + 3 * row['importance'])))
    return round(.35 * row['priority'] + .3 * row['importance'] +
                 .25 * row['activation'] * decay + .1 * row['uncertainty'], 9)


def bounded_text(value, size=500):
    if not isinstance(value, str) or not value.strip() or len(value) > size or len(value.encode('utf-8')) > size * 4:
        raise ValueError('Expected bounded text')
    return value.strip()


def normalized_question(subject):
    """Conservative text identity, not paraphrase/similarity inference."""
    text = unicodedata.normalize('NFC', bounded_text(subject)).casefold()
    text = ' '.join(text.split())
    return unicodedata.normalize('NFC', text.rstrip('?').rstrip() or text)


def question_identity(subject):
    identity = {'kind': 'question', 'subject': normalized_question(subject)}
    return 'question:v1:' + hashlib.sha256(canonical_json(identity).encode()).hexdigest()


class DreamConflict(ValueError):
    pass


class Workspace:
    def __init__(self, database: Database, root, policy=None, clock=time.time):
        self.db, self.root = database, Path(root).resolve()
        self.policy, self.clock = policy or WorkspacePolicy(), clock

    def enqueue_turn(self,path):
        relative=str(Path(path).relative_to(self.root))
        return self.db.create_job('workspace_index',{'path':relative},deduplication_key='index:'+Path(path).stem)

    def revision(self, conn):
        return conn.execute('SELECT global_revision FROM core_state WHERE singleton=1').fetchone()[0]

    def _audit(self, conn, operation, details, revision, target=None):
        return self.db.append_audit(conn, actor='minisa.level0', operation=operation,
            target_category='workspace', target_id=target, details=details, global_revision=revision)

    def _rebalance(self, conn, now, revision):
        rows = conn.execute("SELECT * FROM workspace_units WHERE status != 'resolved' AND (status='active' OR eligible_until>=?)",(now,)).fetchall()
        eligible = [r for r in rows if r['activation'] * 2 ** (-max(0, now-r['activated_at']) / (self.policy.half_life*(1+3*r['importance']))) >= .05]
        ranked = sorted(eligible, key=lambda r: (-score(r, now, self.policy), r['unit_id']))
        selected = {r['unit_id'] for r in ranked[:self.policy.active_limit]
                    if r['activation'] * 2 ** (-max(0, now-r['activated_at']) /
                       (self.policy.half_life*(1+3*r['importance']))) >= .05}
        for r in rows:
            status = 'active' if r['unit_id'] in selected else 'cold'
            if status != r['status']:
                conn.execute('UPDATE workspace_units SET status=?,revision=? WHERE unit_id=?',
                             (status, revision, r['unit_id']))
                self._audit(conn, 'attention.tier', {'previous': r['status'], 'new': status}, revision, r['unit_id'])

    def _unit(self, conn, kind, subject, payload, evidence, importance, uncertainty, priority, revision, key):
        if kind not in KINDS:
            raise ValueError('Unsupported unit kind')
        subject = bounded_text(subject)
        if not isinstance(payload, dict) or len(canonical_json(payload).encode()) > 2000:
            raise ValueError('Invalid structured payload')
        for value in (importance, uncertainty, priority):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError('Invalid salience')
        now = self.clock()
        existing = conn.execute('SELECT * FROM workspace_units WHERE dedup_key=?', (key,)).fetchone()
        unit_id = existing['unit_id'] if existing else str(uuid.uuid4())
        conn.execute('''INSERT INTO workspace_units(unit_id,kind,subject,payload_json,evidence_json,created_at,activated_at,activation,importance,uncertainty,priority,status,revision,dedup_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(dedup_key) DO UPDATE SET activated_at=excluded.activated_at,
          activation=1, revision=excluded.revision''',
          (unit_id, kind, subject, canonical_json(payload), canonical_json(evidence), now, now,
           1, importance, uncertainty, priority, 'cold', revision, key))
        stored=conn.execute('SELECT importance FROM workspace_units WHERE unit_id=?',(unit_id,)).fetchone()
        conn.execute('UPDATE workspace_units SET eligible_until=? WHERE unit_id=?',
                     (now+self.policy.half_life*(1+3*stored['importance'])*math.log2(20),unit_id))
        self._audit(conn, 'workspace.activate' if existing else 'workspace.admit',
                    {'kind': kind, 'evidence': evidence, 'subject': subject,
                     'payload': payload, 'importance': importance, 'uncertainty': uncertainty,
                     'priority': priority}, revision, unit_id)
        return unit_id

    def ingest_turn(self, record, source_path):
        """Only final runtime observations; no private model reasoning or raw prompts."""
        if record.get('provenance', 'user') != 'user':
            # Qualification and other non-user traffic stays auditable in the
            # turn record but never becomes genuine workspace evidence.
            return {'filtered': True, 'reason': 'non_user_provenance'}
        if record['status'] not in {'completed', 'failed', 'interrupted'}:
            return {'filtered': True}
        relative = str(Path(source_path).resolve().relative_to(self.root))
        if not relative.startswith('EXPERIENCE/turns/'):
            raise ValueError('Invalid experience source')
        if Path(source_path).is_symlink():
            raise ValueError('Symlink experience source')
        raw = Path(source_path).read_bytes()
        if json.loads(raw) != record:
            raise ValueError('Experience changed during observation')
        observation = {'turn_id': record['id'], 'status': record['status'],
                       'action': record['action'].get('action'),
                       'action_status': record['action'].get('status'),
                       'failure_kind': (record.get('failure') or {}).get('kind'),
                       'user_statement': record['user_input'][:1000],
                       'verification': 'runtime_outcome_only',
                       'mutation_committed': __import__('runtime.services.memory',fromlist=['valid_mutation_receipt']).valid_mutation_receipt(record['action'].get('receipt'))}
        request = {'id': record['id'], 'sha256': hashlib.sha256(raw).hexdigest(), 'observation': observation}
        def mutate(conn):
            revision = self.db.allocate_revision(conn)
            conn.execute('INSERT INTO workspace_evidence VALUES(?,?,?,?,?,?,0)',
                (record['id'], 'turn', relative, request['sha256'], canonical_json(observation), self.clock()))
            significant = bool(record.get('failure')) or observation['action_status']=='failed' or observation['action'] in {'remember','forget'} or record['user_input'].strip().casefold().rstrip('.!?') not in {'hi','hello','hey','thanks','thank you','ok','okay','yes','no','sure','cool','great','nice','yep','nope','thx','こんにちは','ありがとう','はい','いいえ','👍','😊'}
            if not significant:
                conn.execute('UPDATE workspace_evidence SET processed=1 WHERE evidence_id=?',(record['id'],))
            failed = record['status'] != 'completed' or observation['action_status'] == 'failed'
            # Fast acknowledgements are historical evidence, not active attention.
            if significant:
                self._unit(conn, 'interrupted_task' if failed else 'experience',
                    ('Interrupted turn: ' if failed else 'User concern: ')+record['user_input'][:220],
                    {'turn_id': record['id']}, [record['id']], .9 if failed else .4,
                    .8 if failed else .3, .9 if failed else .4, revision, 'turn:' + record['id'])
            self._rebalance(conn, self.clock(), revision)
            audit = self._audit(conn, 'experience.index', {'evidence': record['id']}, revision)
            return {'revision': revision}, revision, audit
        return self.db.execute_idempotent('workspace:turn:' + record['id'], request, mutate)[0]

    def _question(self, conn, change, revision):
        """Keep one open question identity while retaining all supplied support.

        Scan open questions as well as new keys so pre-repair rows keep their UUID
        and references. Existing historical duplicates are not silently deleted.
        Exact application idempotency remains owned by execute_idempotent.
        """
        subject = bounded_text(change['subject'])
        normalized = normalized_question(subject)
        identity = question_identity(subject)
        rows = conn.execute("SELECT * FROM workspace_units WHERE kind='question' "
                            "AND status IN ('active','cold') ORDER BY created_at,unit_id").fetchall()
        existing = next((row for row in rows if normalized_question(row['subject']) == normalized), None)
        if existing is None:
            key = identity
            prior = conn.execute('SELECT * FROM workspace_units WHERE dedup_key=?', (key,)).fetchone()
            if prior:
                if prior['kind'] != 'question' or normalized_question(prior['subject']) != normalized:
                    raise ValueError('Question identity hash collision')
                # A resolved episode must never be implicitly reopened by ON CONFLICT.
                key += ':episode:' + str(revision)
            return self._unit(conn, 'question', subject, {'interpretation': True},
                              sorted(change['evidence']), .5, .5, .5, revision, key)
        previous = json.loads(existing['evidence_json'])
        evidence = sorted(set(previous) | set(change['evidence']))
        added = sorted(set(evidence) - set(previous))
        now = self.clock()
        eligible_until = now + self.policy.half_life * (1 + 3 * existing['importance']) * math.log2(20)
        conn.execute('UPDATE workspace_units SET evidence_json=?,activated_at=?,activation=1,'
                     'eligible_until=?,revision=? WHERE unit_id=?',
                     (canonical_json(evidence), now, eligible_until, revision, existing['unit_id']))
        self._audit(conn, 'workspace.question_enrich' if added else 'workspace.activate',
                    {'question_identity': identity, 'added_evidence': added,
                     'evidence_count': len(evidence),
                     'previous_evidence_sha256': hashlib.sha256(canonical_json(previous).encode()).hexdigest(),
                     'evidence_sha256': hashlib.sha256(canonical_json(evidence).encode()).hexdigest()},
                    revision, existing['unit_id'])
        return existing['unit_id']

    def attention(self, query=''):
        now, budget = self.clock(), self.policy.context_bytes
        terms = set(query.lower().split())
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM workspace_units WHERE status='active'").fetchall()
        rows=[r for r in rows if r['activation']*2**(-max(0,now-r['activated_at'])/(self.policy.half_life*(1+3*r['importance'])))>=.05]
        ranked = sorted(rows, key=lambda r: (-(score(r, now, self.policy) +
                        .15 * bool(terms & set(r['subject'].lower().split()))), r['unit_id']))
        result = []
        for row in ranked:
            value = {'id': row['unit_id'], 'kind': row['kind'], 'subject': row['subject'],
                     'payload': json.loads(row['payload_json']), 'evidence': json.loads(row['evidence_json']),
                     'score': score(row, now, self.policy), 'authority': 'non_authoritative'}
            size = len(canonical_json(value).encode())
            if size <= budget:
                result.append(value)
                budget -= size
        return result

    def sample_storage(self):
        now=self.clock()
        with self.db.transaction(immediate=True) as conn:
            row=conn.execute('SELECT * FROM cognitive_metrics').fetchone()
            if row['sampled_at'] and now-row['sampled_at']<3600: return
            size=conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]
            conn.execute('UPDATE cognitive_metrics SET baseline_bytes=CASE WHEN baseline_bytes=0 THEN ? ELSE baseline_bytes END,last_bytes=?,sampled_at=? WHERE singleton=1',(size,size,now))

    def maintain(self):
        with self.db.transaction(immediate=True) as conn:
            # Avoid a revision/write on every idle tick when no tier changed.
            rows = conn.execute("SELECT * FROM workspace_units WHERE status != 'resolved' AND (status='active' OR eligible_until>=?)",(self.clock(),)).fetchall()
            before = [(r['unit_id'], r['status']) for r in rows]
            revision = self.revision(conn) + 1
            self._rebalance(conn, self.clock(), revision)
            after = list(conn.execute("SELECT unit_id,status FROM workspace_units WHERE status != 'resolved' AND (status='active' OR eligible_until>=?)",(self.clock(),)))
            if before != [tuple(r) for r in after]:
                self.db.allocate_revision(conn)

    def interrupt(self, source, fingerprint, evidence_id, priority):
        bounded_text(source, 80); bounded_text(fingerprint, 160)
        if type(priority) not in (int, float) or not math.isfinite(priority) or not 0 <= priority <= 1:
            raise ValueError('Invalid interrupt priority')
        if priority < self.policy.interrupt_threshold:
            return False
        now = self.clock()
        with self.db.transaction(immediate=True) as conn:
            if not conn.execute('SELECT 1 FROM workspace_evidence WHERE evidence_id=?', (evidence_id,)).fetchone():
                raise ValueError('Missing interrupt provenance')
            old = conn.execute('SELECT * FROM cognitive_interrupts WHERE fingerprint=?', (fingerprint,)).fetchone()
            recent = conn.execute('SELECT count(*) FROM cognitive_interrupts WHERE last_raised>?',
                                 (now-self.policy.interrupt_cooldown,)).fetchone()[0]
            if (old and now-old['last_raised'] < self.policy.interrupt_cooldown) or recent >= self.policy.interrupt_limit:
                return False
            revision = self.db.allocate_revision(conn)
            conn.execute('INSERT INTO cognitive_interrupts VALUES(?,?,?,?,?) ON CONFLICT(fingerprint) '
                         'DO UPDATE SET last_raised=excluded.last_raised,priority=excluded.priority,evidence_id=excluded.evidence_id',
                         (fingerprint, source, now, priority, evidence_id))
            self._unit(conn, 'dependency', source, {'interrupt': fingerprint}, [evidence_id],
                       priority, .8, priority, revision, 'interrupt:' + fingerprint)
            self._rebalance(conn, now, revision)
            self._audit(conn, 'interrupt.raised', {'source': source, 'priority': priority,
                                                'evidence': evidence_id}, revision)
            return True

    def pressure_components(self,conn):
        now=self.clock()
        pending=conn.execute('SELECT count(*),max(created_at) FROM workspace_evidence WHERE processed=0').fetchone()
        rows=conn.execute("SELECT * FROM workspace_units WHERE status='active'").fetchall()
        state=conn.execute('SELECT * FROM dream_state').fetchone()
        important=sum(r['importance']>=.8 for r in rows)
        conflicts=sum(r['kind']=='contradiction' for r in rows)
        errors=sum(r['kind']=='interrupted_task' for r in rows)
        uncertainty=sum(r['uncertainty']*2**(-max(0,now-r['activated_at'])/86400) for r in rows)
        # Time multiplies existing work, never creates work by itself.
        has_work=pending[0]>0
        elapsed=max(0,now-state['last_success']) if state['last_success'] else 0
        return {'evidence':min(30,.5*pending[0]),'importance':min(15,2*important),
                'contradictions':min(20,3*conflicts),'errors':min(10,2*errors),
                'uncertainty':min(10,.3*uncertainty),
                'load':min(5,.2*sum(score(r,now,self.policy) for r in rows)),
                'fragmentation':min(5,max(0,len(rows)-self.policy.active_limit//2)*.2),
                'reflection_age':min(5,elapsed/86400) if has_work else 0}

    def pressure(self, conn=None):
        if conn is None:
            with self.db.connect(readonly=True) as connection: return self.pressure(connection)
        return round(min(100,sum(self.pressure_components(conn).values())),6)

    def schedule(self):
        """One persistent dream, cooldown and hourly invocation budgets across restarts."""
        now = self.clock()
        with self.db.transaction(immediate=True) as conn:
            state = conn.execute('SELECT * FROM dream_state').fetchone()
            pending = conn.execute("SELECT job_id FROM jobs WHERE job_type='dream' AND status IN ('pending','leased')").fetchone()
            if pending:
                return pending[0]
            unprocessed = conn.execute('SELECT count(*),max(created_at) FROM workspace_evidence WHERE processed=0').fetchone()
            idle = unprocessed[0] and now-(unprocessed[1] or now) >= self.policy.idle_seconds
            calls = state['calls'] if state['window_start'] and (now-state['window_start']) < 3600 else 0
            if (not unprocessed[0] or now < state['next_allowed'] or
                calls >= self.policy.calls_per_hour or now < (state['recovery_after'] if 'recovery_after' in state.keys() else 0) or
                (self.pressure(conn) < self.policy.pressure_threshold and not idle)):
                return None
            job_id = str(uuid.uuid4())
            conn.execute("INSERT INTO jobs(job_id,job_type,payload_json,status,created_at,deduplication_key) VALUES(?, 'dream', ?, 'pending', ?, ?)",
                         (job_id, canonical_json({'trigger': 'idle' if idle else 'pressure'}), datetime.fromtimestamp(now, timezone.utc).isoformat(), 'dream:'+job_id))
            self._audit(conn, 'dream.scheduled', {'trigger': 'idle' if idle else 'pressure',
                                                'pressure': self.pressure(conn)}, self.revision(conn), job_id)
            return job_id

    def snapshot(self):
        from runtime.cognition.dream_context import capture
        return capture(self)

    def _starvation_cursor(self, conn):
        """Durable FIFO cursor (created_at, unit_id) from the latest dream result.

        Option F (migration-free): the last committed dream's result_json carries
        'starvation_cursor'. Absent/invalid means 'start from the beginning'.
        """
        row = conn.execute('SELECT result_json FROM dream_results '
                           'ORDER BY resulting_revision DESC, job_id DESC LIMIT 1').fetchone()
        if not row:
            return None
        try:
            value = json.loads(row['result_json'])
        except (ValueError, TypeError):
            return None
        key = value.get('starvation_cursor') if isinstance(value, dict) else None
        if isinstance(key, (list, tuple)) and len(key) == 2:
            return (key[0], key[1])
        return None

    def _starvation_next(self, conn, cursor):
        """Deterministic smallest key strictly greater than cursor, wrapping.

        Concern set P = unresolved persistent questions, ordered by
        (created_at, unit_id). Pure function of current rows + durable cursor;
        no RNG and no wall-clock input.
        """
        rows = conn.execute("SELECT * FROM workspace_units WHERE status!='resolved' "
                            "AND kind='question' ORDER BY created_at, unit_id").fetchall()
        if not rows:
            return None
        if cursor is not None:
            for row in rows:
                if (row['created_at'], row['unit_id']) > (cursor[0], cursor[1]):
                    return dict(row)
        return dict(rows[0])

    def _sqlite_snapshot(self):
        from runtime.cognition.dream_context import BUDGETS, compact_question, unit_view, json_budget
        with self.db.transaction() as conn:
            revision=self.revision(conn)
            evidence=[dict(r) for r in conn.execute('SELECT * FROM workspace_evidence WHERE processed=0 ORDER BY created_at DESC,evidence_id LIMIT ?', (self.policy.snapshot_evidence,))]
            units=[dict(r) for r in conn.execute("SELECT * FROM workspace_units WHERE status!='resolved' AND (status='active' OR importance>=.8) ORDER BY importance DESC,priority DESC,unit_id LIMIT 64")]
            protected=None
            if self.policy.starvation_slots>=1:
                protected=self._starvation_next(conn, self._starvation_cursor(conn))
        for row in evidence: row['observation']=json.loads(row.pop('observation_json'))
        for row in units:
            row['payload']=json.loads(row.pop('payload_json'))
            row['evidence']=json.loads(row.pop('evidence_json'))
        result={'revision':revision,'evidence':[],'units':[]}
        for row in evidence:
            if len(canonical_json(result['evidence']+[row]).encode())<=BUDGETS['evidence']:
                result['evidence'].append(row)
        current_ids = [row['evidence_id'] for row in result['evidence']]
        # Normal lane, unchanged: priority-ordered admission into BUDGETS['units'].
        normal_rows = []
        normal_cards = []
        for row in units:
            # Cost a compact concern, never its unbounded durable support union.
            # Preserve the existing priority order and shared unit byte ceiling.
            row = compact_question(row, current_ids)
            card = unit_view(row, result['evidence'])
            if (len(canonical_json(normal_rows+[row]).encode())<=BUDGETS['units']
                    and len(canonical_json(normal_cards+[card]).encode())<=BUDGETS['units']):
                normal_rows.append(row)
                normal_cards.append(card)
        units_out = normal_rows
        starvation = None
        # Bounded starvation-protection lane: at most one reserved slot. The lane
        # is skipped (no reserve consumed) when the chosen concern is already
        # admitted by the normal lane; the cursor still advances on commit.
        if protected is not None:
            protected['payload']=json.loads(protected.pop('payload_json'))
            protected['evidence']=json.loads(protected.pop('evidence_json'))
            compact = compact_question(protected, current_ids)
            card = unit_view(compact, result['evidence'])
            cost = len(canonical_json(compact).encode())
            card_cost = len(canonical_json(card).encode())
            reserve = self.policy.starvation_reserve
            already_visible = protected['unit_id'] in {r['unit_id'] for r in normal_rows}
            if not already_visible and max(cost, card_cost) > reserve:
                # Bound only the dream copy, never durable subject/support/identity.
                compact = {**compact, 'payload': json_budget(compact['payload'], 512)}
                subject = compact['subject']
                low, high = 0, len(subject)
                while low < high:
                    mid = (low + high + 1) // 2
                    trial = {**compact, 'subject': subject[:mid]}
                    if max(len(canonical_json(trial).encode()),
                           len(canonical_json(unit_view(trial, result['evidence'])).encode())) <= reserve:
                        low = mid
                    else:
                        high = mid - 1
                compact['subject'] = subject[:low]
                card = unit_view(compact, result['evidence'])
                cost = len(canonical_json(compact).encode())
                card_cost = len(canonical_json(card).encode())
            if (cost <= reserve and card_cost <= reserve and not already_visible
                    and len(canonical_json([compact]).encode()) <= BUDGETS['units']
                    and len(canonical_json([card]).encode()) <= BUDGETS['units']):
                # Reserve-first: admit the protected concern, then refill the
                # remainder with the normal lane in unchanged order.
                units_out, cards, _ = [compact], [card], max(cost, card_cost)
                for row in normal_rows:
                    candidate_card = unit_view(row, result['evidence'])
                    if (len(canonical_json(units_out+[row]).encode())<=BUDGETS['units']
                            and len(canonical_json(cards+[candidate_card]).encode())<=BUDGETS['units']):
                        units_out.append(row)
                        cards.append(candidate_card)
            starvation = {'unit_id': protected['unit_id'], 'kind': protected['kind'],
                          'starvation_cursor': [protected['created_at'], protected['unit_id']],
                          'cost_bytes': cost, 'card_bytes': card_cost,
                          'already_visible': already_visible}
        result['units'] = units_out
        if starvation is not None:
            result['_starvation'] = starvation
        return result

    @staticmethod
    def _memory_support(change, snapshot, text=None):
        """The candidate text must be present in the user's own observed statement.

        Sourced, not invented: the text is a substring of a cited observation's
        user_statement (one sentence). Model output cannot create the memory.
        The same check guards the direct memory change and a consolidation
        review; only the candidate text differs.
        """
        evidence = {r['evidence_id']: r for r in snapshot['evidence']}
        candidate = change['fact'] if text is None else text
        fact = ' '.join(str(candidate).split())
        if len(fact) < 8:
            raise ValueError('Memory fact requires a sourced sentence')
        for ref in change['evidence']:
            statement = ' '.join(str((evidence[ref]['observation'] or {}).get('user_statement', '')).split())
            if fact and fact in statement:
                return
        raise ValueError('Memory fact is not supported by cited observations')

    @staticmethod
    def _memory_terms(text):
        return set(re.findall(r"[a-z0-9']+", str(text).casefold())) - MEMORY_STOPWORDS

    @staticmethod
    def _sourced_memory_fact(snapshot, evidence_ids, fact=None):
        """Recover the user-sourced sentence(s) cited by an accepted proposal.

        Prefers the sentence(s) that contain the recorded fact; falls back to the
        full cited user statements. The model cannot inject unsourced text.
        """
        observations = {r['evidence_id']: (r['observation'] or {})
                        for r in snapshot['evidence'] if r['evidence_id'] in set(evidence_ids)}
        statements = [' '.join(str(o.get('user_statement', '')).split())
                      for o in observations.values()]
        statements = [s for s in statements if s]
        if not statements:
            return ''
        sentences = []
        for statement in statements:
            for sentence in re.split(r'(?<=[.!?])\s+', statement):
                if len(sentence) >= 8:
                    sentences.append(sentence.strip())
        wanted = ' '.join(str(fact or '').split())
        if wanted:
            matched = [sentence for sentence in sentences if wanted in sentence]
            if matched:
                return ' '.join(dict.fromkeys(matched))[:2000]
        return ' '.join(dict.fromkeys(sentences))[:2000]

    @staticmethod
    def _supporting_observation(snapshot, evidence_ids, fact=None):
        """The cited observation that actually carries the recorded statement."""
        rows = {r['evidence_id']: r for r in snapshot['evidence']}
        wanted = ' '.join(str(fact or '').split())
        fallback = None
        for ref in evidence_ids:
            row = rows.get(ref)
            if row is None:
                continue
            if fallback is None:
                fallback = row
            statement = ' '.join(str((row['observation'] or {}).get('user_statement', '')).split())
            if wanted and wanted in statement:
                return row
        return fallback

    def _persist_memory(self, conn, change, snapshot, revision, fact_text=None):
        """Durably create one sourced conversation-memory record.

        Replaces a redundant review admission: the model proposes a memory
        (type 'memory', or review/memory_consolidation) whose claim is supported
        by the cited observation; the runtime sources the fact from the user's
        own statement and persists one memory record. No model-supplied text is
        stored verbatim.
        """
        evidence = change['evidence']
        candidate = change['fact'] if fact_text is None else fact_text
        fact = self._sourced_memory_fact(snapshot, evidence, candidate)
        if not fact:
            raise ValueError('Memory requires a sourced user statement')
        support = self._supporting_observation(snapshot, evidence, candidate)
        source = support['source_path'] if support else ''
        record_hash = support['sha256'] if support else ''
        title = ((support['observation'] or {}).get('user_statement', '')
                 if support else fact)[:80]
        title = ' '.join(title.split()) or 'Sourced memory'
        identity = MEMORY_NOTE_IDENTITY + hashlib.sha256(
            canonical_json({'fact': fact, 'evidence': sorted(evidence)}).encode()).hexdigest()
        record = {'subject': title, 'fact': fact, 'evidence': sorted(evidence),
                  'source_path': source, 'source_sha256': record_hash, 'revision': revision,
                  'authority': 'runtime_sourced_user_claim'}
        self.db.append_memory(conn, identity, record, revision)
        return identity

    def validate(self, proposal, snapshot):
        if not isinstance(proposal, dict) or set(proposal) != {'changes'}:
            raise ValueError('Dream requires only structured changes; private reasoning is forbidden')
        changes = proposal['changes']
        if not isinstance(changes, list) or len(changes) > self.policy.proposal_limit or len(canonical_json(proposal).encode()) > 16000:
            raise ValueError('Proposal budget exceeded')
        evidence = {r['evidence_id']: r for r in snapshot['evidence']}
        units = {r['unit_id'] for r in snapshot['units']}
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError('Invalid change')
            kind = change.get('type')
            if any(str(change.get(field,'')).casefold() in {'core','identity','creator','values','core_identity','instructions'} for field in ('type','target','kind')):
                raise ValueError('identity/core drift attempt')
            schemas = CHANGE_FIELDS
            if kind not in schemas or set(change) != schemas[kind]:
                raise ValueError('Unsupported change schema')
            refs = change['evidence']
            if not isinstance(refs, list) or not refs or len(refs) > 16 or len(set(refs)) != len(refs) or any(r not in evidence for r in refs):
                raise ValueError('Missing snapshot evidence')
            if kind == 'unit':
                if change['kind'] not in KINDS:
                    raise ValueError('Invalid unit kind')
                bounded_text(change['subject'])
            elif kind == 'resolve':
                if change['unit_id'] not in units:
                    raise ValueError('Unknown workspace unit')
            elif kind == 'association':
                if (not isinstance(change['unit_ids'], list) or len(change['unit_ids']) != 2 or
                    len(set(change['unit_ids'])) != 2 or any(r not in units for r in change['unit_ids'])):
                    raise ValueError('Invalid association references')
            elif kind == 'review':
                if change['target'] not in PROTECTED | {'memory_consolidation', 'memory_tier', 'goal'}:
                    raise ValueError('Unsupported review target')
                bounded_text(change['claim'])
            elif kind == 'memory':
                # A durable, non-protected learning target: a sourced conversation
                # fact. It never touches CORE, beliefs, personality or self-model.
                bounded_text(change['title'])
                bounded_text(change['fact'])
                self._memory_support(change, snapshot)
            else:
                bounded_text(change['situation']); bounded_text(change['procedure'])
                observations = [evidence[r]['observation'] for r in refs]
                # Semantic generalization is still a candidate; verified success is limited
                # to runtime-observed actions, never an arbitrary model assertion.
                if (len(observations) < 3 or any(o.get('action_status') != 'completed' or
                    o.get('action') not in {'remember','forget'} or not o.get('mutation_committed') for o in observations) or
                    len({o['turn_id'] for o in observations}) < 3):
                    raise ValueError('Procedure requires three independent successful action observations')
        return deepcopy(changes)

    def apply(self, job, owner, snapshot, proposal):
        changes = self.validate(proposal, snapshot)
        request = {'snapshot': snapshot, 'proposal': proposal}
        def mutate(conn):
            now = self.clock()
            lease = conn.execute('SELECT * FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            if (not lease or lease['status'] != 'leased' or lease['lease_owner'] != owner or
                datetime.fromisoformat(lease['lease_expires_at']).timestamp() <= now):
                raise DreamConflict('lease_lost')
            if self.revision(conn) != snapshot['revision']:
                raise DreamConflict('sqlite_revision')
            for row in snapshot['evidence']:
                path = self.root / row['source_path']
                if (path.is_symlink() or not path.resolve().is_relative_to(self.root) or
                    hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']):
                    raise DreamConflict('historical_evidence_changed')
            from runtime.cognition.dream_context import validate_sources
            validate_sources(self,snapshot)
            # Capture the pre-proposal selection: this transaction may admit new
            # questions, which were never presented by the current dream.
            cursor_before = self._starvation_cursor(conn)
            selected = self._starvation_next(conn, cursor_before) if self.policy.starvation_slots else None
            presented = selected is not None and selected['unit_id'] in {r['unit_id'] for r in snapshot['units']}
            revision = self.db.allocate_revision(conn)
            accepted, review = [], []
            review_work = []
            review_index = None
            def associate_review(change):
                nonlocal review_index
                from runtime.cognition.reviews import outstanding, associate
                if review_index is None:
                    review_index = {r['intent_id']: r for r in outstanding(conn)}
                review_work.append(associate(conn, self._audit, job['job_id'],
                                             change, revision, review_index))
            for change in changes:
                kind = change['type']
                key = hashlib.sha256(canonical_json(change).encode()).hexdigest()
                consolidated = False
                if kind == 'unit':
                    if change['kind'] == 'question':
                        self._question(conn, change, revision)
                    else:
                        self._unit(conn, change['kind'], change['subject'], {'interpretation': True},
                                   change['evidence'], .5, .5, .5, revision, 'dream:'+key)
                elif kind == 'resolve':
                    review.append({**change,'admission':'semantic_resolution_requires_review'})
                elif kind == 'memory':
                    # Sourced, durable, non-protected learning. Provenance is
                    # re-derived from the cited observation, never trusted verbatim.
                    memory_id = self._persist_memory(conn, change, snapshot, revision)
                    self._audit(conn, 'dream.memory_persisted',
                                {'target': 'memory', 'evidence': change['evidence'],
                                 'memory_id': memory_id, 'source': 'user_statement'},
                                revision, job['job_id'])
                    consolidated = True
                elif kind == 'association':
                    self._unit(conn, 'association', 'Candidate evidence association',
                               {'unit_ids': change['unit_ids'], 'verification': 'unverified'},
                               change['evidence'], .5, .6, .4, revision, 'dream:'+key)
                elif kind == 'procedure':
                    observations={r['evidence_id']:r['observation'] for r in snapshot['evidence']}
                    instruction='procedure: '+change['situation']+' -> '+change['procedure']
                    supported=all(instruction.casefold() in observations[e].get('user_statement','').casefold() for e in change['evidence'])
                    if not supported:
                        review.append({**change,'admission':'procedure_relevance_requires_review'})
                        associate_review(review[-1])
                        continue
                    # Evidence count does not validate causality. Keep learned procedures
                    # non-executable and retrievable as hypotheses pending further evidence.
                    conn.execute(
                        'INSERT OR IGNORE INTO learned_procedures '
                        '(procedure_id, global_revision, created_at, last_used_at, title, '
                        'situation, procedure, evidence_json, usage_count, confidence, '
                        'provenance_json, source_evidence_ids, signature) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (key, revision, now, None, change['situation'], change['situation'],
                         change['procedure'], canonical_json(sorted(change['evidence'])), 0, .5,
                         canonical_json({'authority': 'dream_hypothesis', 'executable': False,
                                         'admission': 'evidence_supported_not_causal'}),
                         canonical_json(sorted(change['evidence'])), key))
                elif kind == 'review' and change['target'] == 'memory_consolidation':
                    # A consolidation review whose claim is directly supported by
                    # the cited observation is durable, sourced learning rather
                    # than protected-state review. An unsupported claim stays
                    # review-only and never creates a memory.
                    try:
                        self._memory_support(change, snapshot, change['claim'])
                    except ValueError:
                        review.append(change)
                    else:
                        memory_id = self._persist_memory(conn, change, snapshot, revision,
                                                         change['claim'])
                        self._audit(conn, 'dream.memory_persisted',
                                    {'target': change['target'], 'evidence': change['evidence'],
                                     'memory_id': memory_id, 'source': 'user_statement'},
                                    revision, job['job_id'])
                        consolidated = True
                else:
                    review.append(change)
                if consolidated:
                    accepted.append(change)
                    continue
                if kind not in {'review','resolve'}:
                    accepted.append(change)
                if kind in {'review','resolve'}:
                    associate_review(change)
                else:
                    self._audit(conn, 'dream.change', change, revision, job['job_id'])
            for row in snapshot['evidence']:
                conn.execute('UPDATE workspace_evidence SET processed=1 WHERE evidence_id=?', (row['evidence_id'],))
            self._rebalance(conn, now, revision)
            # Bounded starvation-protection lane: advance the durable rotation
            # cursor on commit only (a deferred/retried dream never reaches
            # apply). Use the pre-proposal selection under sqlite_revision.
            starvation_key = list(cursor_before) if self.policy.starvation_slots and cursor_before is not None else None
            if self.policy.starvation_slots >= 1:
                if presented:
                    starvation_key = [selected['created_at'], selected['unit_id']]
                    self._audit(conn, 'attention.protected',
                                {'unit_id': selected['unit_id'], 'kind': selected['kind'],
                                 'cursor_before': list(cursor_before) if cursor_before else None,
                                 'cursor_after': starvation_key}, revision)
            result = {'source_revision': snapshot['revision'], 'revision': revision,
                      'accepted': accepted, 'review': review, 'records_considered': len(snapshot['evidence'])}
            if starvation_key is not None:
                result['starvation_cursor'] = starvation_key
            if review_work:
                result['review_work'] = review_work
            conn.execute(
                'INSERT INTO dream_results '
                '(job_id, source_revision, resulting_revision, result_json, created_at) '
                'VALUES(?,?,?,?,?)',
                (job['job_id'], snapshot['revision'], revision, canonical_json(result), now)
            )
            conn.execute("UPDATE jobs SET status='completed',completed_at=?,result_json=?,lease_owner=NULL,lease_expires_at=NULL WHERE job_id=?",
                         (str(now), canonical_json(result), job['job_id']))
            conn.execute('UPDATE dream_state SET last_success=?,failures=0,deferrals=0,recovery_after=0,last_failure_category=NULL,reflected_revision=? WHERE singleton=1', (now,revision))
            audit = self._audit(conn, 'dream.committed', {'accepted': len(accepted), 'review': len(review)}, revision, job['job_id'])
            return result, revision, audit
        from runtime.storage import writer_lock
        with writer_lock(self.root,blocking=False):
            return self.db.execute_idempotent('dream:apply:'+job['job_id'], request, mutate)[0]

    def pending_reviews(self):
        """Read-only outstanding admitted work; no terminal review semantics."""
        from runtime.cognition.reviews import outstanding
        with self.db.connect(readonly=True) as conn:
            return outstanding(conn)

    def procedures(self, query, limit=5):
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute('SELECT * FROM learned_procedures ORDER BY global_revision DESC LIMIT 1000').fetchall()
        terms=set(bounded_text(query[:500]).lower().split())
        ranked=sorted(rows,key=lambda r:(-len(terms & set(r['situation'].lower().split())),r['procedure_id']))
        return [{**dict(r), 'status': 'hypothesis', 'executable': False} for r in ranked
                if terms & set(r['situation'].lower().split())][:max(1,min(limit,10))]

    def sourced_memories(self, query, limit=5):
        """Durable learning admitted from the user's own observed statements.

        Read-only projection of the sourced memory records. The stored fact is
        the user's own sentence, re-derived at admission time; this projection
        never rewrites or invents memory content. A record is recalled only
        when the query shares a meaningful word with its own sourced text.
        """
        wanted = self._memory_terms(query)
        if not wanted:
            return []
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute('SELECT memory_id, details_json, revision FROM memory_records '
                                'ORDER BY revision DESC, created_at DESC LIMIT 500').fetchall()
        scored = []
        for row in rows:
            try:
                details = json.loads(row['details_json'])
            except (ValueError, TypeError):
                continue
            if not isinstance(details, dict):
                continue
            fact = str(details.get('fact') or '').strip()
            if not fact:
                continue
            subject = str(details.get('subject') or '').strip()
            overlap = wanted & self._memory_terms(subject + ' ' + fact)
            if not overlap:
                continue
            scored.append((len(overlap), int(row['revision'] or 0), str(row['memory_id']), {
                'id': str(row['memory_id']),
                'source': str(details.get('source_path') or ''),
                'status': 'sourced_user_claim',
                'confidence': 'unverified_user_claim',
                'subject': subject or fact[:80],
                'fact': fact,
                'evidence': list(details.get('evidence') or []),
                'revision': int(row['revision'] or 0),
            }))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [item[3] for item in scored[:max(0, int(limit))]]
