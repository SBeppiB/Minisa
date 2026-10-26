"""Runtime-owned awake turn observations; no replay, learning, or delivery claims."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from uuid import uuid4
from runtime.storage import atomic_write, serialized
from runtime.cognition.context import bounded, runtime_action_context


class TurnStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'EXPERIENCE/turns'

    def paths(self):
        return sorted(self.path.glob('turn_*.json'))

    def read(self, path):
        if path.is_symlink() or path.parent != self.path:
            raise ValueError('Invalid turn path')
        record = json.loads(path.read_text())
        if (record.get('schema') != 1 or record.get('id') != path.stem
                or record.get('status') not in {'in_progress', 'completed', 'failed', 'interrupted'}):
            raise ValueError('Invalid turn record; operator review required')
        return record

    def publish(self, record, replace=True):
        receipt = atomic_write(self.path / (record['id'] + '.json'),
                            json.dumps(record, ensure_ascii=False, indent=2) + '\n',
                            root=self.root, replace=replace)
        if record['status']!='in_progress' and getattr(self,'workspace',None):
            try:
                self.workspace.enqueue_turn(self.path/(record['id']+'.json'))
            except Exception:
                # Final historical evidence already committed. Startup reconciliation
                # recovers the outbox gap; never misreport this as an action rollback.
                logging.getLogger('minisa.turns').exception('Final turn committed; workspace indexing deferred')
        return receipt

    @serialized
    def recover(self):
        # Caller owns the service lease or whole-turn writer lock. Never replay actions.
        for path in self.paths():
            record = self.read(path)
            if record['status'] == 'in_progress':
                record.update(status='interrupted', failure={'kind': 'interrupted',
                    'message': 'No final outcome was durably observed. Unrecorded effects remain unknown.'})
                self.publish(record)

    @serialized
    def begin(self, user_input, *, provenance='user'):
        if provenance not in {'user', 'qualification'}:
            raise ValueError('Invalid turn provenance')
        paths = self.paths()
        sequence = max((self.read(p)['sequence'] for p in paths), default=0) + 1
        record = {'schema': 1, 'id': 'turn_' + uuid4().hex, 'sequence': sequence,
                  'started_at': datetime.now(timezone.utc).isoformat(),
                  'status': 'in_progress', 'stage': 'admitted', 'user_input': user_input,
                  'provenance': provenance,
                  'answer': None, 'returned_text': None, 'failure': None,
                  'action': {'status': 'not_started'}, 'cognitive_outcome': {'status': 'not_started'},
                  'experience': None, 'reflection': None,
                  'delivery': 'not_observed'}
        self.publish(record, replace=False)
        return record

    def recent(self, exclude=None):
        records = [self.read(p) for p in self.paths() if p.stem != exclude]
        records.sort(key=lambda r: (r['sequence'], r['id']))
        result = []
        for r in records[-4:]:
            result.append({'turn_id': r['id'], 'status': r['status'],
                'user_input': bounded(r['user_input'], 650),
                # Answers remain in the durable audit record, never in model history.
                'answer': None,
                'action': runtime_action_context(r.get('action', {})),
                'failure': bounded(r['failure'], 150),
                'action_status': r['action'].get('status', 'unknown')})
        return bounded(result, 6000)
