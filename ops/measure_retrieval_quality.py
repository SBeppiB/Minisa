"""Measure keyword retrieval quality for durable sourced memories.

The learning cycle is only useful if a later conversation can find what was
learned. This harness measures two axes for different kinds of query:

  exact        the query reuses the content words of the stored fact
  morphological the query uses another form of the same word (prepare/preparation)
  paraphrase   the query uses different words with the same meaning
  vague        the query shares almost no content word with the fact

  retrieval  the durable memory is returned by the lexical recall projection
  answer     the runtime answers from that memory without asking the model
  context    the memory is handed to the model as context for the answer

It measures the current implementation; it never changes it. The report
is written to a file (default /tmp/minisa-retrieval-report.json) and printed
as a summary. Nothing outside the temporary directory is touched.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import Database  # noqa: E402
from runtime.cognition.engine import CognitionEngine  # noqa: E402
from runtime.cognition.workspace import Workspace, WorkspacePolicy  # noqa: E402
from runtime.services.memory import LocalMemoryService  # noqa: E402

# (label, stored fact as the user said it, {category: query})
CASES = (
    ('tea',
     'The tea ceremony uses two spoons of matcha and water at 80 degrees.',
     {'exact': 'what do you know about the tea ceremony',
      'morphological': 'how do we prepare the tea ceremony',
      'paraphrase': 'how do we brew the green powder ritual',
      'vague': 'what did we agree on for the ritual'}),
    ('bike',
     'The bike chain needs a drop of chain oil every 500 kilometres.',
     {'exact': 'what do you know about the bike chain',
      'morphological': 'how do we lubricate the bike chain',
      'paraphrase': 'how do we service the two wheel vehicle drivetrain',
      'vague': 'what did we agree on for the vehicle'}),
    ('deadline',
     'The quarterly report is due on the 15th of March.',
     {'exact': 'what do you know about the quarterly report',
      'morphological': 'when is the quarterly reporting finished',
      'paraphrase': 'when is the paper due',
      'vague': 'what did we agree on for the paperwork'}),
    ('grinder',
     'The coffee grinder burrs are replaced every 18 months.',
     {'exact': 'what do you know about the coffee grinder',
      'morphological': 'how do we maintain the coffee grinding machine',
      'paraphrase': 'when do we swap the mill components',
      'vague': 'what did we agree on for the machine'}),
    ('flight',
     'The flight to Porto departs at 07:15 from gate B12.',
     {'exact': 'what do you know about the flight to Porto',
      'morphological': 'when does the Porto airplane depart',
      'paraphrase': 'when does the plane leave for the Portuguese city',
      'vague': 'what did we agree on for the trip'}),
    ('budget',
     'The project budget ceiling is 12 thousand euros.',
     {'exact': 'what do you know about the project budget',
      'morphological': 'what is the project spending limit',
      'paraphrase': 'how much may the project cost',
      'vague': 'what did we agree on for the money'}),
)

CATEGORIES = ('exact', 'morphological', 'paraphrase', 'vague')


def build_store(directory: Path):
    """A disposable workspace holding the memories the way the dream stores them."""
    root = directory / 'workspace'
    (root / 'CORE').mkdir(parents=True)
    for name in ('identity', 'values', 'goals', 'personality', 'instructions'):
        (root / 'CORE' / f'{name}.md').write_text(f'# {name}\n\nMeasurement fixture.\n',
                                                  encoding='utf-8')
    database = Database(root / 'data/minisa.db')
    database.initialize()
    workspace = Workspace(database, root,
                          WorkspacePolicy(pressure_threshold=0.1, cooldown=1,
                                          idle_seconds=1, calls_per_hour=10),
                          clock=lambda: 1_800_000_000.0)
    for index, (_, fact, _) in enumerate(CASES):
        details = {'subject': fact[:80], 'fact': fact, 'evidence': [f'turn_{index}'],
                   'source_path': f'EXPERIENCE/turns/turn_{index}.json',
                   'source_sha256': '0' * 64, 'revision': index + 1,
                   'authority': 'runtime_sourced_user_claim'}
        with database.connect() as conn:
            database.append_memory(conn, f'memory_{index}', details, index + 1)
    return workspace, root


class _NoReasoner:
    """The harness measures the runtime paths, not the model."""

    def reason(self, context):
        return {'proposed_action': 'respond', 'interpretation': 'measurement'}

    def respond(self, context, reasoning):
        return {'kind': 'conversation', 'text': 'model answer'}


def build_engine(root: Path, workspace: Workspace):
    memory = LocalMemoryService(root)
    memory.recall = lambda query, limit, semantic=False: []
    engine = CognitionEngine(root, _NoReasoner(), memory)
    engine.workspace = workspace
    return engine


def measure():
    with tempfile.TemporaryDirectory(prefix='minisa-retrieval-') as name:
        workspace, root = build_store(Path(name))
        engine = build_engine(root, workspace)
        results = []
        for label, fact, queries in CASES:
            for category in CATEGORIES:
                query = queries[category]
                found = workspace.sourced_memories(query, limit=5)
                hit = any(fact.casefold() in str(item.get('fact', '')).casefold()
                          for item in found)
                answer = engine._normal_note_response(query)
                answered = bool(answer) and fact.casefold() in answer.casefold()
                context = engine.build_context(query)
                in_context = any(fact.casefold() in str(entry.get('fact', '')).casefold()
                                 for entry in context.get('memories', []))
                results.append({'memory': label, 'category': category, 'query': query,
                                'hit': hit, 'returned': len(found), 'answered': answered,
                                'in_context': in_context})
    summary = {}
    for category in CATEGORIES:
        rows = [row for row in results if row['category'] == category]
        hits = sum(1 for row in rows if row['hit'])
        answered = sum(1 for row in rows if row['answered'])
        in_context = sum(1 for row in rows if row['in_context'])
        summary[category] = {'hits': hits, 'total': len(rows),
                             'hit_rate': round(hits / len(rows), 3) if rows else 0.0,
                             'answered': answered,
                             'answer_rate': round(answered / len(rows), 3) if rows else 0.0,
                             'in_context': in_context,
                             'context_rate': round(in_context / len(rows), 3) if rows else 0.0}
    total_hits = sum(1 for row in results if row['hit'])
    total_answered = sum(1 for row in results if row['answered'])
    total_context = sum(1 for row in results if row['in_context'])
    summary['overall'] = {'hits': total_hits, 'total': len(results),
                          'hit_rate': round(total_hits / len(results), 3) if results else 0.0,
                          'answered': total_answered,
                          'answer_rate': round(total_answered / len(results), 3) if results else 0.0,
                          'in_context': total_context,
                          'context_rate': round(total_context / len(results), 3) if results else 0.0}
    return {'summary': summary, 'results': results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='/tmp/minisa-retrieval-report.json')
    parser.add_argument('--min-exact-hit-rate', type=float, default=None,
                        help='exit non-zero when the exact-query hit rate drops below this')
    arguments = parser.parse_args()
    report = measure()
    Path(arguments.output).write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps(report['summary'], indent=1))
    misses = [row for row in report['results'] if not row['hit']]
    print(f'misses: {len(misses)}')
    for row in misses:
        print(f"  [{row['category']}] {row['memory']}: {row['query']}")
    exact = report['summary']['exact']['hit_rate']
    if arguments.min_exact_hit_rate is not None and exact < arguments.min_exact_hit_rate:
        print(f'FAIL exact hit rate {exact} below {arguments.min_exact_hit_rate}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
