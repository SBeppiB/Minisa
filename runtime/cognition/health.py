"""Read-only deterministic Memory Doctor. Semantic suspicion is never proof."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from core.db import Database
from runtime.cognition.persistence import read_document
from runtime.cognition.workspace import Workspace


def diagnose(workspace, model_enabled=None):
    w, findings = workspace, []
    def report(code, count, category='deterministic'):
        if count:
            findings.append({'code':code,'count':count,'category':category})
    def parsed(value,expected):
        try:
            result=json.loads(value)
            if not isinstance(result,expected):raise ValueError('Wrong JSON type')
            if expected is list and any(not isinstance(ref,str) for ref in result):raise ValueError('Invalid reference')
            return result
        except (ValueError,TypeError):
            report('invalid_structured_record',1)
            return expected()
    now = w.clock()
    with w.db.connect(readonly=True) as conn:
        integrity = conn.execute('PRAGMA quick_check').fetchone()[0]
        report('database_integrity', integrity != 'ok')
        report('broken_foreign_keys', len(conn.execute('PRAGMA foreign_key_check').fetchall()))
        units = conn.execute('SELECT * FROM workspace_units').fetchall()
        evidence = {r['evidence_id']:r for r in conn.execute('SELECT * FROM workspace_evidence')}
        report('workspace_bloat', max(0,sum(r['status']=='active' for r in units)-w.policy.active_limit))
        report('stale_active_units', sum(r['status']=='active' and now-r['activated_at'] > 7*86400 for r in units))
        report('broken_evidence_links', sum(ref not in evidence for r in units for ref in parsed(r['evidence_json'],list)))
        report('missing_provenance', sum(not parsed(r['evidence_json'],list) for r in units))
        report('unresolved_experiences', sum(not r['processed'] for r in evidence.values()))
        report('contradiction_candidates', sum(r['kind']=='contradiction' and r['status']!='resolved' for r in units), 'semantic_candidate')
        report('failed_dream_jobs',conn.execute("SELECT count(*) FROM jobs WHERE job_type='dream' AND status='failed'").fetchone()[0])
        report('future_revisions',sum(r['revision']>w.revision(conn) for r in units))
        report('repeated_dream_failures',conn.execute('SELECT failures FROM dream_state').fetchone()[0])
        report('overactivation', sum(r['activation'] >= .99 and now-r['activated_at'] > 30*86400 for r in units))
        review = conn.execute("SELECT details_json FROM audit_events WHERE operation='dream.failed'").fetchall()
        report('identity_drift_rejections',sum(parsed(r[0],dict).get('category')=='identity_drift' for r in review))
        ids={r['unit_id'] for r in units}
        associations=[parsed(r['payload_json'],dict).get('unit_ids',[]) for r in units if r['kind']=='association']
        invalid_associations=sum(not isinstance(refs,list) or any(not isinstance(ref,str) for ref in refs) for refs in associations)
        report('invalid_association_payload',invalid_associations)
        report('broken_association_references',sum(ref not in ids for refs in associations if isinstance(refs,list) for ref in refs if isinstance(ref,str)))
        procedures=conn.execute('SELECT * FROM learned_procedures').fetchall()
        report('broken_procedure_evidence',sum(ref not in evidence for r in procedures for ref in parsed(r['evidence_json'],list)))
        report('orphan_cognitive_units',sum(not parsed(r['evidence_json'],list) or all(ref not in evidence for ref in parsed(r['evidence_json'],list)) for r in units))
        report('unresolved_stale_units',sum(r['status']!='resolved' and now-r['activated_at']>30*86400 for r in units))
        state=conn.execute('SELECT * FROM dream_state').fetchone()
        report('deferral_storm',state['deferrals'] if state['deferrals']>=5 else 0)
        report('dream_recovery_circuit',now<state['recovery_after'])
        report('dream_model_disabled',model_enabled is False)
        report('ingestion_requires_review',conn.execute("SELECT count(*) FROM jobs WHERE job_type='workspace_index' AND status='review_required'").fetchone()[0])
        metrics=dict(conn.execute('SELECT * FROM cognitive_metrics').fetchone())
        logical_bytes=conn.execute('PRAGMA page_count').fetchone()[0]*conn.execute('PRAGMA page_size').fetchone()[0]
        growth=max(0,logical_bytes-metrics['baseline_bytes']) if metrics['baseline_bytes'] else 0
        report('storage_growth_review',growth>64*1024*1024)
    missing, changed = 0, 0
    for r in evidence.values():
        path = w.root/r['source_path']
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(w.root):
            missing += 1
        elif hashlib.sha256(path.read_bytes()).hexdigest() != r['sha256']:
            changed += 1
    report('missing_historical_evidence',missing)
    report('changed_historical_evidence',changed)
    hashes, broken_links = {}, 0
    paths = list((w.root/'MEMORY/active').glob('memory_*.md'))
    for path in paths:
        if path.is_symlink():
            report('symlink_memory',1)
            continue
        if path.stat().st_size>128000:
            report('oversized_memory_skipped',1)
        try: text = path.read_text()
        except (UnicodeError,OSError):
            report('unreadable_memory',1)
            continue
        # Exact byte duplicates are deterministic. Similarity requires a semantic job.
        digest = hashlib.sha256(text.encode()).hexdigest()
        hashes[digest] = hashes.get(digest,0)+1
        import re
        for ref in re.findall(r'\[\[(memory_\d+)(?:\|[^\]]*)?\]\]',text):
            broken_links += not (w.root/'MEMORY/active'/f'{ref}.md').exists() and not (w.root/'MEMORY/archive'/f'{ref}.md').exists()
    report('duplicate_memory_bytes',sum(n-1 for n in hashes.values()))
    report('broken_memory_links',broken_links)
    try:
        read_document(w.root)
    except (ValueError,OSError,TypeError):
        report('invalid_cognitive_history',1)
    size = w.db.path.stat().st_size
    report('database_size_review',size > 256*1024*1024)
    return {'status':'review' if findings else 'ok','findings':findings,
            'database_bytes':size,'logical_database_bytes':logical_bytes,
            'wal_bytes':Path(str(w.db.path)+'-wal').stat().st_size if Path(str(w.db.path)+'-wal').exists() else 0,
            'growth_since_baseline_bytes':growth,'storage_baseline_sampled_at':metrics['sampled_at'],
            'scheduling':{'model_enabled':model_enabled,'recovery_after':state['recovery_after'],
                          'next_allowed':state['next_allowed'],'failures':state['failures'],'deferrals':state['deferrals']},
            'units':len(units),'evidence':len(evidence),
            'semantic_diagnostics':'candidate counts only; model analysis requires a bounded dream',
            'destructive_maintenance':False}


def main():
    parser=argparse.ArgumentParser(description='Read-only Minisa cognitive health')
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(diagnose(Workspace(Database(args.database),args.root)),indent=2))

if __name__=='__main__':
    main()
