"""Opt-in experimental reflector. No Letta dependency or canonical writer here.

The trusted development controller supplies an OS-isolated command. The sidecar
gets bounded values only, never a Workspace, database, root or manager. The
existing DreamWorker remains the only validator/commit coordinator.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from core.db import canonical_json
from runtime.cognition.context import system_context
from runtime.cognition.dream import DreamModel, dream_prompt
from runtime.cognition.dream_context import model_snapshot, TOTAL_BYTES
from runtime.cognition.workspace import CHANGE_FIELDS, KINDS, PROTECTED


class ReadOnlySnapshot:
    """In-memory views only; every read returns an independent copy."""
    def __init__(self, snapshot):
        self._snapshot = deepcopy(model_snapshot(snapshot))
        if len(canonical_json(self._snapshot).encode()) > TOTAL_BYTES:
            raise ValueError('Dream context byte budget exceeded')

    def get_workspace(self): return deepcopy(self._snapshot.get('units', []))
    def get_relevant_experiences(self): return deepcopy(self._snapshot.get('evidence', []))
    def get_relevant_memories(self): return deepcopy(self._snapshot.get('memory_context', []))
    def get_beliefs(self): return deepcopy(self._snapshot.get('protected', {}).get('beliefs', {}))
    def get_self_model(self): return deepcopy(self._snapshot.get('protected', {}).get('self_model', {}))
    def get_recent_reflections(self): return deepcopy(self._snapshot.get('reflections', []))


def proposal_contract(snapshot):
    """Field names/enums come from the same definitions the validator consumes.

    This describes syntax, not permission to commit; semantic validation and
    review boundaries still run exclusively inside Workspace.validate/apply.
    """
    return {
        'envelope': {'changes': 'array; no other fields'},
        'exact_change_fields': {k: sorted(v) for k,v in CHANGE_FIELDS.items()},
        'unit_kinds': sorted(KINDS),
        'review_targets': sorted(PROTECTED | {'memory_consolidation','memory_tier','goal'}),
        'evidence_ids': [r['evidence_id'] for r in snapshot['evidence']],
        'workspace_unit_ids': [r['unit_id'] for r in snapshot['units']],
    }


class LettaModel:
    """Same propose(snapshot, core) boundary as DreamModel; disposable use only."""
    def __init__(self, command, model, disposable_root, timeout=20):
        root=Path(disposable_root).resolve()
        if (not root.is_relative_to(Path('/tmp')) or
            not (root/'.minisa-letta-disposable').is_file()):
            raise ValueError('Marked disposable /tmp environment required')
        if not command or command[0]!='/usr/bin/bwrap' or not model or not 1<=timeout<=20:
            raise ValueError('Explicit isolated bounded Letta configuration required')
        # A developer-controlled launcher, never model-supplied executable text.
        self.command=tuple(command);self.model=model;self.timeout=timeout
        self.usage={}

    def propose(self, snapshot, core):
        bounded=ReadOnlySnapshot(snapshot)._snapshot
        payload={'snapshot':bounded,'model':self.model,
                 'system':system_context({'core':deepcopy(core)}),
                 'prompt':dream_prompt(bounded)+'\nvalidator_contract:\n'+canonical_json(proposal_contract(bounded))}
        started=time.monotonic()
        process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,env={'PATH':'/usr/bin:/bin'},close_fds=True,start_new_session=True)
        try:
            output,_=process.communicate(canonical_json(payload),timeout=self.timeout)
        except BaseException as error:
            # Reap the whole process group: SDK, App Server and any descendants.
            os.killpg(process.pid,signal.SIGKILL);process.communicate()
            if isinstance(error,subprocess.TimeoutExpired):
                raise TimeoutError('Letta wall-clock deadline exceeded') from error
            raise
        if process.returncode:
            raise OSError('Letta runtime unavailable or failed')
        if len(output.encode())>65536:
            raise ValueError('Letta response budget exceeded')
        envelope=json.loads(output)
        text=envelope['response']
        if not isinstance(text,str) or len(text.encode())>16000:
            raise ValueError('Letta proposal budget exceeded')
        # No stripping fences, field renaming, reference substitution or retries.
        proposal=json.loads(text)
        metrics=envelope.get('metrics',{})
        self.usage={'total_seconds':time.monotonic()-started,
                    'processing_ms':metrics.get('processing_ms'),
                    'startup_ms':metrics.get('startup_ms'),
                    'controller_idle_rss_bytes':metrics.get('controller_idle_rss_bytes'),
                    'controller_idle_cpu_percent':metrics.get('controller_idle_cpu_percent'),
                    'runtime_idle_rss_bytes':metrics.get('runtime_idle_rss_bytes'),
                    'runtime_idle_processes':metrics.get('runtime_idle_processes'),
                    'runtime_idle_cpu_percent':metrics.get('runtime_idle_cpu_percent'),
                    'agent_id':metrics.get('agent_id'),
                    'tool_calls':metrics.get('tool_calls',[])}
        return proposal


def select_runtime(runtime, *, host='', model='', command=None, disposable_root=None):
    """Explicit prototype selection; production server configuration is unchanged."""
    if runtime=='direct_model': return DreamModel(host,model)
    if runtime=='letta': return LettaModel(command,model,disposable_root)
    raise ValueError('Unknown dream runtime')
