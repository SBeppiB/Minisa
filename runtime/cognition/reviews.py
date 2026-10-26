"""Derived outstanding work, not a reviewer workflow or mutation authority.

Immutable admissions/associations own provenance; exact applications retain their
job-scoped receipts. No terminal review lifecycle is implemented here. A future
disposition feature must extend this projection before enabling such decisions.
"""
import hashlib
import json
from core.db import canonical_json


def review_definition(change):
    kind = change['type']
    if kind == 'resolve':
        return {'type': kind, 'unit_id': change['unit_id']}
    if kind == 'review':
        # The claim is opaque: no case/whitespace/punctuation/paraphrase folding.
        return {'type': kind, 'target': change['target'], 'claim': change['claim']}
    if kind == 'procedure':
        return {'type': kind, 'situation': change['situation'],
                'procedure': change['procedure'],
                'admission': 'procedure_relevance_requires_review'}
    raise ValueError('Unsupported review intent')


def review_intent(change):
    return 'review:v1:' + hashlib.sha256(canonical_json(review_definition(change)).encode()).hexdigest()


def outstanding(conn):
    """Complete read-only projection, including immutable legacy admissions.

    Do not LIMIT this lookup to context-sized/recent history: an old outstanding
    intent remains the same decision. Cost grows with retained review history.
    Cold targets remain applicable; absent/resolved targets do not imply approval.
    """
    unresolved = {row[0] for row in conn.execute("SELECT unit_id FROM workspace_units WHERE status!='resolved'")}
    groups = {}
    rows = conn.execute("SELECT event_id,operation,details_json FROM audit_events "
                        "WHERE operation IN ('dream.review','dream.review_associated') "
                        # A batch can admit and associate at one revision; random
                        # UUID order must not put an association before its root.
                        "ORDER BY CASE WHEN operation='dream.review' THEN 0 ELSE 1 END,global_revision,event_id")
    for event in rows:
        details = json.loads(event['details_json'])
        change = details['proposal'] if event['operation'] == 'dream.review_associated' else details
        definition = review_definition(change)
        if definition['type'] == 'resolve' and definition['unit_id'] not in unresolved:
            continue
        identity = review_intent(change)
        group = groups.get(identity)
        if group is None:
            if event['operation'] == 'dream.review_associated':
                raise ValueError('Review association lacks its admission')
            group = groups[identity] = {'intent_id': identity, 'definition': definition,
                'first_event_id': event['event_id'], 'event_count': 0, 'evidence': set()}
        if group['definition'] != definition:
            raise ValueError('Review intent hash collision')
        if event['operation'] == 'dream.review_associated' and (
                details['intent_id'] != identity or details['first_event_id'] != group['first_event_id']):
            raise ValueError('Review association identity mismatch')
        group['event_count'] += 1
        group['evidence'].update(change['evidence'])
    return [{**group, 'evidence': sorted(group['evidence'])} for group in groups.values()]


def associate(conn, audit, job_id, change, revision, index):
    """Append one truthful occurrence inside the caller's existing transaction."""
    identity = review_intent(change)
    definition = review_definition(change)
    group = index.get(identity)
    previous = set(group['evidence']) if group else set()
    if group is not None and group['definition'] != definition:
        raise ValueError('Review intent hash collision')
    if group is None:
        event = audit(conn, 'dream.review', change, revision, job_id)
        group = index[identity] = {'intent_id': identity, 'definition': definition,
            'first_event_id': event, 'event_count': 0, 'evidence': []}
        outcome = 'admitted'
    else:
        event = audit(conn, 'dream.review_associated', {'proposal': change,
            'intent_id': identity, 'first_event_id': group['first_event_id']}, revision, job_id)
        outcome = 'associated'
    evidence = previous | set(change['evidence'])
    group['evidence'] = sorted(evidence)
    group['event_count'] += 1
    # Receipt metadata never repeats the growing historical evidence union.
    return {'intent_id': identity, 'first_event_id': group['first_event_id'],
            'event_id': event, 'outcome': outcome,
            'added_evidence_count': len(evidence-previous), 'evidence_count': len(evidence)}


def awareness(work):
    """Small optional cards. Handles and historical evidence grant no authority."""
    cards = []
    for item in work[:4]:
        definition = item['definition']
        card = {'intent_id': item['intent_id'], 'first_event_id': item['first_event_id'],
                'type': definition['type'], 'interpretation': 'outstanding_admitted_work',
                'evidence_count': len(item['evidence'])}
        if definition['type'] == 'resolve':
            card['target_unit_id'] = definition['unit_id']
        elif definition['type'] == 'review':
            card['target'] = definition['target']
            card['claim_preview'] = definition['claim'].encode()[:200].decode(errors='ignore')
        else:
            card['situation_preview'] = definition['situation'].encode()[:200].decode(errors='ignore')
        if len(canonical_json(cards+[card]).encode()) <= 1500:
            cards.append(card)
    return cards
