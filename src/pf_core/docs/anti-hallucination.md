# Anti-Hallucination: Constrain Input, Don't Filter Output

A pattern guide for tools that use LLMs to find or cite real information.

## The lesson

LLM-with-search hybrids (Perplexity Sonar, Bing Chat, etc.) feel like they produce ground-truth URLs because they "have web access." They don't. They synthesize their output from retrieval signals — and when retrieval comes up empty, they often invent a URL rather than return nothing.

A consumer project ran this experiment in May 2026:

- Query-driven search model (Perplexity Sonar Pro) → extraction pipeline
- Defenses: URL pattern detection in the prompt, post-hoc URL liveness check, post-hoc LLM content verifier
- Test result: **28–57% of yielded results had fabricated URLs** matching the exact patterns the prompt warned against
- Root cause: when Perplexity returned `NONE`, the system fell back to `gpt-4o-mini` (no web search), which invented URLs from training data in the very pattern the search prompt warned against — `apnews.com/article/<keyword-slug>-<year>` etc.

The defenses caught most fabrications. But the unit economics were terrible: ~$0.25 per kept entry once you counted verification costs and human review time on the rejects.

## The wrong architecture

```
LLM with search → emits URLs as part of its output
              → bolt on URL liveness check
              → bolt on content verifier
              → bolt on URL pattern detector
              → reject the fakes after the fact
```

Each filter catches some fabrications. Together they create the illusion of safety while the root architecture — *the LLM is being asked to produce URLs* — keeps generating them. You pay to produce junk and pay again to verify it.

## The right architecture

```
Real search API (Brave, Google, Bing, etc.)
    → list of real URLs from a real index
    → fetch each URL (e.g. pf_core.utils.article_fetch)
    → LLM extracts facts / answers from the fetched text
    → URLs come from the index, never from the LLM
```

The LLM never invents URLs because it never produces URLs. It only reads what's in front of it.

This is the pattern in pf-core's `brave.search()` + `article_fetch()` pair. It's also how good RAG systems work: the retriever owns the URLs; the LLM only generates prose grounded in the retrieved content.

## When the right architecture isn't possible

Sometimes you're stuck with LLM-as-search (cost constraints, niche domains where Brave's index is weak, etc.). The mitigations, in order of leverage:

1. **Never use a non-web-search model as fallback.** A chat-only model (`gpt-4o-mini`, `claude-3-haiku`) called with a search-shaped prompt will hallucinate URLs from training data. If your primary search model fails, return the failure — don't ask a model without a real index to "try anyway." Cost of zero yield > cost of fabricated yield.

2. **Demote the LLM's URL output to a candidate list, not a citation.** Treat what the LLM emits as queries to verify, not facts to publish. Run each URL through liveness check and content verifier before it reaches the user.

3. **Pattern-match for fabrication signatures, but in code, not in the prompt.** Telling the LLM "don't emit URLs that look like X" doesn't work — it emits them anyway. Filter them out programmatically in a post-hoc check. (See the table below for known patterns.)

4. **Strict structured-output validators.** If the LLM is supposed to pick from a closed set (slugs, IDs, classifications), enforce that at parse time. Open-ended string outputs get cheated; closed-vocab outputs can't.

## Known fabricated URL patterns

These signatures show up in LLM output when the model is improvising rather than retrieving. Worth filtering even after a real search API, in case some bad URL slipped through:

| Pattern | Real shape | Fabricated shape |
|---|---|---|
| AP News articles | `/article/<32-char-hex>` | `/article/<keyword-slug>-<year>` |
| Reuters articles | slug ends in alphanumeric hash | slug ends in `idUSKBN<XYZ>` (placeholder) |
| NPR articles | `/2025/03/15/g-s1-12345/` (real ID) | `/nx-s1-<NNNNNNN>/` or `/g-s1-<NNNNNNN>/` (synthetic) |
| Congress.gov bills | `/119th-congress/` for 2025+ | `/118th-congress/` (118th ended Jan 2025) |
| Wayback timestamps | almost never midnight UTC | timestamp ending in exactly `000000` |
| Newsletter posts | `/p/real-slug-from-actual-post` | `/p/<topic>-<date>` invented from content description |

These are not exhaustive — collect more from your own audit. The common shape is: *the URL looks plausible because the LLM extrapolated the platform's URL format from training data, but the specific path doesn't exist*.

## The fallback-model trap

The most dangerous failure mode is this:

```
Primary search model (Perplexity) → returns NONE due to over-restrictive prompt
                                  ↓
Fallback model (gpt-4o-mini) → has no real search; invents URLs
                              ↓
Output looks like real search results
```

If you have a fallback ladder, **every model in the ladder must have real web search**. A chat model in the fallback chain is worse than no fallback at all — it produces output indistinguishable from real search results to your downstream pipeline.

The consumer project's fix was to set `SEARCH_MODEL_FALLBACK=""` (empty; disabled). When the primary search fails, the system records `NONE` and moves on. Better to lose yield on already-failing queries than to gain hallucinated yield from a model that can't actually search.

## Don't tell the LLM not to hallucinate URLs

It is tempting to write prompts like:

> "If you find yourself about to emit a URL of the form
> `apnews.com/article/<keyword-slug>-<year>`, OMIT IT. That pattern
> indicates a fabricated URL."

This is the wrong layer. Two failure modes:

1. **The LLM still emits them.** Models are not reliable at self-monitoring against pattern rules in their own output.
2. **The LLM gets paranoid and emits nothing.** Tightening the "don't make stuff up" instruction can flip the model into refusing to return real URLs that happen to look similar to the forbidden pattern. The consumer project discovered this happened: the anti-hallucination prompt made Perplexity return `NONE` instead of real AP articles, which triggered the fallback-model trap above.

Move pattern-checking into deterministic Python code that runs after the LLM call. Let the LLM do what it's good at; let regex do what regex is good at.

## Inputs you can constrain

For each LLM-touching step, ask: **what can come from a deterministic source instead?**

| Concern | Deterministic source |
|---|---|
| URLs to cite | Real search API (`pf_core.clients.brave`) or curated source inline links |
| Article content | Live fetch + extract (`pf_core.utils.article_fetch`) |
| Calendar dates | `pf_core.utils.relative_dates` for relative phrases; HTML metadata for absolute |
| Allowed categories / tags / slugs | Closed vocabulary in config + Pydantic `Literal[...]` validator |
| Foreign keys / entity IDs | Lookup table; reject if not present |
| Domain-specific format (date, currency, ID) | Stdlib parsers |

The remaining LLM jobs — summarization, judgment, classification within a closed set — are the things LLMs actually do well.

## See also

- `pf_core.clients.brave` — real search API client
- `pf_core.utils.article_fetch` — fetch + extract real article content
- `pf_core.utils.relative_dates` — resolve LLM-emitted date phrases in Python
- `pf_core/docs/llm-validation.md` — Pydantic-based output validation
- `pf_core/docs/project-portability.md` — keep domain values in config so prompts stay generic
