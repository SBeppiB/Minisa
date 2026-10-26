"""Optimistic multi-store snapshots: bounded context, complete source fingerprints.

Capture under the existing root writer boundary and a SQLite read transaction.
Reason without locks. Reacquire root + SQL commit boundary and check all versions,
file fingerprints and directory membership. New memory/reflection members defer.
"""
from pathlib import Path
import hashlib
import json
import re
from runtime.storage import writer_lock
from runtime.cognition.persistence import read_document
from runtime.belief_manager import BeliefManager
from runtime.personality.manager import PersonalityManager
from runtime.self_model.manager import SelfModelManager
from runtime.state.manager import StateManager
from runtime.core.loader import CoreLoader

BUDGETS={'protected':4000,'evidence':6000,'units':5000,'memory_context':2500,
         'reflections':1500,'procedures':1500,'goals':1000,'contradictions':1000}
# Prompt plus authoritative CORE must stay below a conservative byte ceiling.
# This is byte admission, not an invented exact tokenizer count; provider ctx=16384.
TOTAL_BYTES=24000
# Support exposure is independent of durable relationship cardinality. Current
# snapshot evidence comes first, then canonical historical IDs; neither grants
# authority beyond snapshot.evidence. No persistent row is changed.
QUESTION_SUPPORT_LIMIT=8
QUESTION_SUPPORT_BYTES=512
# Reserved sub-budget inside BUDGETS['units'] for the single bounded
# starvation-protection slot (kind='question' only). A protected question card
# is bounded by compact_question; STARVATION_RESERVE covers the worst case.
STARVATION_RESERVE=2600


def compact_question(row, current_ids):
    if row['kind'] != 'question' or row['status'] == 'resolved':
        return row
    refs = set(row['evidence'])
    current = [ref for ref in current_ids if ref in refs]
    ordered = current + sorted(refs - set(current))
    selected = records_budget(ordered[:QUESTION_SUPPORT_LIMIT], QUESTION_SUPPORT_BYTES)
    return {**row, 'evidence': selected, 'evidence_count': len(refs),
            'evidence_omitted_count': len(refs)-len(selected)}


def utf8_prefix(value,bytes_limit):
    return value.encode('utf-8')[:bytes_limit].decode('utf-8',errors='ignore')


def json_budget(value,budget):
    """Preserve complete JSON structure; shorten strings on UTF-8 boundaries."""
    encoded=lambda v:json.dumps(v,ensure_ascii=False,separators=(',',':')).encode()
    if len(encoded(value))<=budget: return value
    if isinstance(value,str):
        low,high=0,len(value)
        while low<high:
            mid=(low+high+1)//2
            if len(encoded(value[:mid]))<=budget: low=mid
            else: high=mid-1
        return value[:low]
    result=[] if isinstance(value,list) else {}
    if isinstance(value,(dict,list)):
        items=value.items() if isinstance(value,dict) else enumerate(value)
        for key,item in items:
            item=json_budget(item,max(2,budget//3))
            candidate={**result,key:item} if isinstance(result,dict) else result+[item]
            if len(encoded(candidate))<=budget: result=candidate
    return result


def records_budget(records,budget):
    """Admit whole records; never truncate reference IDs or source paths."""
    result=[]
    for record in records:
        if len(json.dumps(result+[record],ensure_ascii=False,separators=(',',':')).encode())<=budget:
            result.append(record)
    return result


def source_manifest(root):
    files=['STATE/cognition.json','STATE/current_state.md','STATE/continuity.md',
           'PERSONALITY/current.json','SELF_MODEL/current.json','REFLECTION/consciousness/current.json']
    files += ['CORE/'+n+'.md' for n in ('identity','values','goals','personality','instructions')]
    members={}
    for directory,pattern in [('MEMORY/active','memory_*.md'),('REFLECTION/self_reflections','*.md')]:
        members[directory]=sorted(str(p.relative_to(root)) for p in (root/directory).glob(pattern))
        files += members[directory]
    fingerprints={}
    for relative in files:
        path=root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Invalid snapshot source')
        if not path.exists(): fingerprints[relative]=None; continue
        # Stream hashes; oversized records are never dumped into model context.
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(65536),b''): digest.update(chunk)
        fingerprints[relative]=digest.hexdigest()
    return {'files':fingerprints,'members':members}


def capture(workspace):
    from runtime.cognition.workspace import DreamConflict
    w=workspace
    with writer_lock(w.root,blocking=False):
        before=source_manifest(w.root)
        snapshot=w._sqlite_snapshot()
        document=read_document(w.root)
        core=CoreLoader(w.root).load()
        protected={'beliefs':BeliefManager(w.root).load(document),
                   'personality':PersonalityManager(w.root).snapshot(document),
                   'self_model':SelfModelManager(w.root).snapshot(document),
                   'state':StateManager(w.root).snapshot(document),
                   'continuity':StateManager(w.root).load_continuity()}
        snapshot['protected']={key:json_budget(value,budget) for (key,value),budget in zip(protected.items(),(900,900,900,700,200))}
        snapshot['cognitive_revision']=document['revision']
        terms=set(re.findall(r'\w+', ' '.join(r['observation'].get('user_statement','') for r in snapshot['evidence']).casefold()))
        def relevance(text): return len(terms & set(re.findall(r'\w+',text.casefold())))
        memories=[]
        for relative in before['members']['MEMORY/active']:
            path=w.root/relative
            if path.stat().st_size>128000: continue
            text=path.read_text()
            memories.append({'path':relative,'claim':utf8_prefix(text,1200),'authority':'saved_claim'})
        memories.sort(key=lambda r:(-relevance(r['claim']),r['path']))
        snapshot['memory_context']=records_budget(memories[:16],BUDGETS['memory_context'])
        reflections=[]
        for relative in before['members']['REFLECTION/self_reflections'][-32:]:
            path=w.root/relative
            if path.stat().st_size<=128000:
                text=path.read_text()
                text=re.sub(r'<think>.*?</think>','',text,flags=re.S|re.I)
                # Only named published reflection fields; no raw transcript dump.
                fields={}
                for label in ('Observation','Implications','Uncertainty'):
                    match=re.search(r'(?:^|\n)(?:#+\s*)?'+label+r':?\s*\n?(.*?)(?=\n#+ |\Z)',text,re.S|re.I)
                    if match: fields[label]=utf8_prefix(match.group(1).strip(),250)
                reflections.append({'path':relative,'structured_evidence_review':json_budget(fields,800),'authority':'unverified_model_reflection'})
        reflections.sort(key=lambda r:(-relevance(json.dumps(r['structured_evidence_review'],ensure_ascii=False)),r['path']))
        snapshot['reflections']=records_budget(reflections[:4],BUDGETS['reflections'])
        with w.db.connect(readonly=True) as conn:
            rows=[dict(r) for r in conn.execute('SELECT * FROM learned_procedures ORDER BY global_revision DESC LIMIT 100')]
        rows.sort(key=lambda r:(-relevance(r['situation']),r['procedure_id']))
        snapshot['procedures']=records_budget([{**r,'executable':False,'status':'hypothesis'} for r in rows[:5]],BUDGETS['procedures'])
        snapshot['goals']={'core_goals':json_budget(core.get('goals',''),400),
                           'workspace_goals':records_budget([r for r in snapshot['units'] if r['kind']=='goal'],500)}
        snapshot['contradictions']=records_budget([r for r in snapshot['units'] if r['kind']=='contradiction'],BUDGETS['contradictions'])
        after=source_manifest(w.root)
        if before!=after: raise DreamConflict('sources_changed_during_capture')
        with w.db.connect(readonly=True) as conn:
            from runtime.cognition.reviews import outstanding, awareness
            review_cards = awareness(outstanding(conn))
            if w.revision(conn)!=snapshot['revision']: raise DreamConflict('sqlite_changed_during_capture')
        snapshot['source_versions']=before
        snapshot['_core']=core
        # Optional awareness cannot displace concern/evidence context or raise
        # the established ceiling. Admission uses complete history, never cards.
        while review_cards:
            candidate = {**snapshot, 'pending_reviews': review_cards}
            if len(json.dumps(model_snapshot(candidate),ensure_ascii=False,separators=(',',':')).encode())<=TOTAL_BYTES:
                snapshot['pending_reviews'] = review_cards
                break
            review_cards.pop()
        if len(json.dumps(model_snapshot(snapshot),ensure_ascii=False,separators=(',',':')).encode())>TOTAL_BYTES:
            raise ValueError('Dream context byte budget exceeded')
        return snapshot


def validate_sources(workspace,snapshot):
    from runtime.cognition.workspace import DreamConflict
    if source_manifest(workspace.root)!=snapshot.get('source_versions'):
        raise DreamConflict('source_content_or_membership_changed')
    if read_document(workspace.root)['revision']!=snapshot['cognitive_revision']:
        raise DreamConflict('cognitive_revision_changed')


def model_snapshot(snapshot):
    # Hash manifests/core and the starvation-selection ledger are validation
    # metadata, not model input tokens.
    return {k:v for k,v in snapshot.items() if k not in {'source_versions','_core','_starvation'}}


def unit_view(row, evidence):
    """One bounded presentation card; shared by admission and presentation."""
    current = {item['evidence_id'] for item in evidence}
    refs = row.get('evidence', [])
    item = {key: row[key] for key in ('unit_id', 'kind', 'subject', 'status', 'payload') if key in row}
    item['current_evidence_ids'] = [ref for ref in refs if ref in current]
    historical = [ref for ref in refs if ref not in current]
    item['historical_evidence_ids_not_legal_for_this_dream'] = records_budget(historical, 512)
    item['historical_evidence_count'] = row.get('evidence_count', len(refs)) - len(item['current_evidence_ids'])
    if row.get('kind') == 'question' and row.get('status') != 'resolved':
        for key in ('dedup_key', 'evidence_count', 'evidence_omitted_count'):
            if key in row: item[key] = row[key]
        item['role'] = 'persistent_open_question'
        item['resolve_admission'] = 'review_only'
        stop = set('a an the is are was were be been to of for and or if its it in on at '
                   'we can how what when do did does this that with without still has '
                   'have had our which will would should as by from now'.split())
        def terms(text): return set(re.findall(r'\w+', text.casefold())) - stop
        links = []
        for record in evidence:
            shared = sorted(terms(row['subject']) & terms(record['observation'].get('user_statement', '')))
            if shared:
                links.append({'evidence_id': record['evidence_id'], 'shared_terms': shared})
        item['lexical_current_evidence_links_not_proof'] = records_budget(links, 512)
    else:
        item['role'] = 'experience_observation' if row.get('kind') == 'experience' else row.get('kind')
    return item


def proposal_view(snapshot):
    """Level 1 cards preserve the snapshot's exclusive reference authority.

    Native admission checks both row and card costs. Keep the defensive bound for
    externally supplied snapshots too; projection never mutates its input.
    """
    view = model_snapshot(snapshot)
    projected = [unit_view(row, snapshot.get('evidence', [])) for row in snapshot.get('units', [])]
    admitted = records_budget(projected, BUDGETS['units'])
    view['open_question_targets'] = [row for row in admitted if row.get('role') == 'persistent_open_question']
    view['units'] = [row for row in admitted if row.get('role') != 'persistent_open_question']
    return view
