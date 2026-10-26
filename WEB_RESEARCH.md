# Candidate web research tool

This is a separate, candidate-only addition. It does not change the staged
dream-recovery release manifest and has not been deployed.

The `runtime.research` module uses the standard library only. Tavily search is
the preferred optional provider (`TAVILY_API_KEY`). Its search request is basic,
bounded to five results, and excludes answer synthesis. If there is no key,
`MINISA_SEARXNG_URL` selects an existing SearXNG-compatible endpoint. That
instance must enable JSON output; many public instances disable it. The code
does not install or host a search service.

Both providers use bounded requests; direct page retrieval accepts only public
HTTP(S) IPs, validates DNS addresses, connects to the validated IP, checks each
redirect, limits redirects/time/response size, and accepts HTML or plain text.
Page scripts and other active elements are discarded. Page text is untrusted
and never executed. A saved note is a short supplied summary and up to eight
key points, with source URL and retrieval time; it does not copy the article.
Same-source notes are deduplicated. Existing active `MEMORY` notes are used, so
dream snapshots can admit them through the existing bounded memory context and
source fingerprint checks. They remain lower-authority saved claims, not
verified facts or instructions.

Example from the candidate root:

```bash
export MINISA_ROOT=/srv/minisa
python -m runtime.research_cli search 'topic to research'
python -m runtime.research_cli fetch 'https://public.example/page'
python -m runtime.research_cli save 'https://public.example/page' \
  --summary 'Short attributed summary.' --point 'First sourced claim.'
```

To use Tavily at zero required spend, create a free key and configure it only in
the service environment (not in TOML, notes, or command arguments). Verify the
account is on the free Researcher plan and that pay-as-you-go/paid overages are
not enabled. Tavily's currently published free plan advertises 1,000 credits per
month without a credit card; requests stop when the allowance is exhausted.
The alternative is an existing SearXNG endpoint configured as
`MINISA_SEARXNG_URL`, with JSON enabled. Do not store either provider secret in
research notes or logs.

After saving, normal dream snapshot construction includes the note under
`memory_context` when relevant to admitted workspace evidence. Confirm later
with a read-only dream status/snapshot diagnostic or use Minisa's existing
`[[Research: title]]` / titled-note lookup. The tool does not force dreams or
bypass workspace evidence admission.
