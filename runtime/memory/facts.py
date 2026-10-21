"""Read-time fact projection. Stored claims are data, never verified authority."""
import re

_MARKER = re.compile(r"^(?:## (Fact|Information)[ \t]*|Fact:[ \t]*(.*))$", re.MULTILINE)
_BOUNDARY = re.compile(r"^(?:#{1,6} .+|Original Input:.*|Reason:.*)$", re.MULTILINE)
_PROVENANCE = {'## Original Input', '## Reason', 'Original Input:', 'Reason:'}


def fact_record(content):
    text = content.replace('\r\n', '\n').replace('\r', '\n')
    matches = list(_MARKER.finditer(text))
    # Only markers before provenance belong to the claim, not quoted user input.
    provenance = re.search(r'^(?:## Original Input|## Reason|Original Input:|Reason:)', text, re.MULTILINE)
    if provenance:
        matches = [m for m in matches if m.start() < provenance.start()]
    if len(matches) != 1:
        return {'fact': '', 'projection_status': 'ambiguous' if matches else 'unsupported',
                'unparsed': text}
    marker = matches[0]
    tail = text[marker.end():].lstrip('\n')
    boundary = _BOUNDARY.search(tail)
    fact = ((marker.group(2) or '') + '\n' + (tail[:boundary.start()] if boundary else tail)).strip()
    extra = ''
    if boundary and boundary.group().strip() not in _PROVENANCE:
        # Preserve unfamiliar sections for review without assigning fact identity.
        extra = tail[boundary.start():]
        end = re.search(r'^(?:## Original Input|## Reason|Original Input:|Reason:)', extra, re.MULTILINE)
        if end:
            extra = extra[:end.start()]
    return {'fact': fact, 'projection_status': 'ambiguous' if extra else 'supported' if fact else 'empty',
            'unparsed': extra.strip()}


def fact_identity(fact):
    """Whole-claim equality; only whitespace is insignificant. Case/punctuation stay."""
    return ' '.join(fact.split())
