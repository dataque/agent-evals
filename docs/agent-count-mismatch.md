
## 6. Proposed solutions — two approaches

The user-reported pair of failures (prose count diverges from card count; "are there any more?" answered wrong) can be fixed at two scopes. Both are documented in full below so the implementing engineer can pick one and code it directly from this section.

### 6.1 Decision overview

| Aspect | Approach A — Minimal (BFF + prompt only) | Approach B — Full pagination (adds domain-service change) |
|---|---|---|
| Files changed | 3 production + 1 test | 4 production + 2 tests |
| Domain-service touched | No | Yes (`RequisitionMatchService.match()`) |
| New tool output fields | `count`, `size`, `page` | `count`, `size`, `page`, `totalAvailable`, `hasMore` |
| "Showing 4 of 17" framing | Not possible | Possible |
| `hasMore` accuracy | Approximated as `count == size` (LLM-derived) | Exact (`PaginatedResource.hasNext`) |
| Self-correcting edge case | Yes — `count == size` with no further matches → user accepts "load more" → next call returns 0 → agent corrects to "those were all" | None — agent's offer matches reality |
| Existing search pagination semantics | Unchanged | Changed: domain service now fetches a fixed pool, filters once, slices in-memory |
| Effort | ~1–2 hours | ~half day including tests |
| Blast radius | Zero outside the BFF agent tool | Affects every caller of `RequisitionMatchService.match()` (today: BFF agent tool + batch nudge processor — neither reads `totalElements`, so behavior-compatible, but worth verifying) |

Pick **A** if the deliverable is "fix the reported bug, ship today." Pick **B** if the team also wants accurate pagination affordances ("Showing 4 of 17", reliable "Show more" pills) and is willing to absorb a domain-service change.

The two approaches share the same prompt-edit pattern; Approach B's prompt is a strict superset of Approach A's. No need to back out A if B is later applied — the field set grows, the existing fields stay.

---

### 6.2 Approach A — Minimal fix (BFF + prompt only)

#### 6.2.1 Files changed

| Path | Change |
|---|---|
| `api-service/bff-service/src/main/java/com/example/svc/domain/tools/requisitionmatching/SuggestRequisitionsOutput.java` | Add 3 fields |
| `api-service/bff-service/src/main/java/com/example/svc/ai/tool/RequisitionMatchingTools.java` | Populate new fields in `suggestRequisitions(...)` |
| `api-service/bff-service/src/main/resources/agents/local/RequisitionMatchingAgent.md` | 3 prompt edits |
| `api-service/bff-service/src/test/java/com/example/svc/ai/tool/RequisitionMatchingToolsTest.java` | Assertions for new fields |

#### 6.2.2 `SuggestRequisitionsOutput.java` — full new content

Replace the file's contents with:

```java
package com.example.svc.domain.tools.requisitionmatching;

import java.util.List;

/**
 * Output of the {@code suggest_requisitions} tool.
 *
 * <p>The {@code count}, {@code size}, and {@code page} fields exist specifically so the
 * agent prompt can reference them by name when composing the user-facing response,
 * instead of asking the LLM to count the {@code matches} array itself (which is prone
 * to context-bias hallucination when prior turns mention a different number).
 *
 * @param matches            current page of role suggestions
 * @param count              {@code matches.size()} — the digit the LLM must echo verbatim
 * @param size               page size used for this call (the requested {@code size}, not {@code matches.size()})
 * @param page               zero-based index of the current page
 * @param requiredNextAction inline directive for the next required tool call
 */
public record SuggestRequisitionsOutput(
        List<RequisitionMatch> matches,
        int count,
        int size,
        int page,
        String requiredNextAction) {}
```

#### 6.2.3 `RequisitionMatchingTools.java` — diff

Locate `suggestRequisitions(...)` (currently `:78-156`). The change is two new local-variable lines plus an updated `return`. Show before/after for the relevant block (`:130-155`):

**Before:**

```java
        List<RequisitionMatchDto> matchDtos = toReturn.getContent();
        Map<String, TalentProfileDto> recruiterProfileMap = resolveRecruiterProfiles(matchDtos.stream()
                .map(dto -> dto.getRequisition() != null ? dto.getRequisition().getRecruiter() : null)
                .toList());

        List<RequisitionMatch> matches = matchDtos.stream()
                .map(dto -> {
                    String recruiterId =
                            dto.getRequisition() != null ? dto.getRequisition().getRecruiter() : null;
                    TalentProfileDto recruiterProfile =
                            recruiterId != null ? recruiterProfileMap.get(recruiterId) : null;
                    ViewRequisitionOutput req =
                            toolMapper.mapToViewRequisitionOutput(dto.getRequisition(), recruiterProfile);
                    return toolMapper.mapToRequisitionMatch(dto, req);
                })
                .toList();

        // Inline directive in the tool's JSON output — the highest-salience position for
        // forcing emit_followups compliance. The agent's response template after
        // suggest_requisitions is short and templated, which makes prompt-side reminders
        // unreliable; this hint sits right next to the matches data the model is reading.
        String requiredNextAction = matches.isEmpty()
                ? "REQUIRED next action: call emit_followups with scenario_id=\"no_matches\" and pills=[\"Update my skills\", \"Show profile gaps\"] before ending the turn. Do not skip — this is the last action of your turn."
                : "REQUIRED next action: call emit_followups with scenario_id=\"matches_returned\" and pills=[\"Show more\", \"Ask about a role\"] before ending the turn. Do not skip — this is the last action of your turn.";

        return new SuggestRequisitionsOutput(matches, requiredNextAction);
```

**After:**

```java
        List<RequisitionMatchDto> matchDtos = toReturn.getContent();
        Map<String, TalentProfileDto> recruiterProfileMap = resolveRecruiterProfiles(matchDtos.stream()
                .map(dto -> dto.getRequisition() != null ? dto.getRequisition().getRecruiter() : null)
                .toList());

        List<RequisitionMatch> matches = matchDtos.stream()
                .map(dto -> {
                    String recruiterId =
                            dto.getRequisition() != null ? dto.getRequisition().getRecruiter() : null;
                    TalentProfileDto recruiterProfile =
                            recruiterId != null ? recruiterProfileMap.get(recruiterId) : null;
                    ViewRequisitionOutput req =
                            toolMapper.mapToViewRequisitionOutput(dto.getRequisition(), recruiterProfile);
                    return toolMapper.mapToRequisitionMatch(dto, req);
                })
                .toList();

        int page = input.page() == null ? 0 : input.page();
        int size = input.size() == null ? 5 : input.size();
        int count = matches.size();

        // Inline directive in the tool's JSON output — the highest-salience position for
        // forcing emit_followups compliance. The agent's response template after
        // suggest_requisitions is short and templated, which makes prompt-side reminders
        // unreliable; this hint sits right next to the matches data the model is reading.
        String requiredNextAction = matches.isEmpty()
                ? "REQUIRED next action: call emit_followups with scenario_id=\"no_matches\" and pills=[\"Update my skills\", \"Show profile gaps\"] before ending the turn. Do not skip — this is the last action of your turn."
                : "REQUIRED next action: call emit_followups with scenario_id=\"matches_returned\" and pills=[\"Show more\", \"Ask about a role\"] before ending the turn. Do not skip — this is the last action of your turn.";

        return new SuggestRequisitionsOutput(matches, count, size, page, requiredNextAction);
```

The existing `log.info("Fetched {} role suggestions for user {}.", toReturn.getContent().size(), userId);` at `:125-128` stays unchanged — it remains the per-call evidence line used by KQL queries.

#### 6.2.4 `RequisitionMatchingAgent.md` — three prompt edits

**Edit 1 (line 71 — pagination clause inside Tool Trigger Rules rule 1).**

Find:

> *"The tool currently accepts `page` (default 0) and `size` (default 5). If the user wants to view more matches, increase the page +1 from the previous page input. If you can't find the previous page number, default to 0 again. Do not fabricate filters — the tool does NOT accept location, level, skills, department, country, or free-text search parameters today. If the user asks for a filter you cannot apply (e.g. "only roles in London"), acknowledge honestly that filtering is not yet supported from chat and still return the current matches."*

Replace with:

> *"The tool currently accepts `page` (default 0) and `size` (default 5). **Only call `suggest_requisitions` again with `page = previous_page + 1` if the previous tool result had `count == size` (the page was full and more may exist). If `count < size`, do NOT call the tool again — the user has already seen everything available. Reuse the previous result.** Do not fabricate filters — the tool does NOT accept location, level, skills, department, country, or free-text search parameters today. If the user asks for a filter you cannot apply (e.g. "only roles in London"), acknowledge honestly that filtering is not yet supported from chat and still return the current matches."*

**Edit 2 (line 85 — intro composition inside the suggest_requisitions non-empty branch).**

Find:

> *"Compose a 1–2 sentence intro that includes ALL of: the exact number of role suggestions returned by the tool, the fact that they appear below this message, and that clicking a role opens its full details on the right side panel. Compose your own phrasing using the actual count from the tool output — do NOT copy a stock template verbatim."*

Replace with:

> *"Compose a 1–2 sentence intro that includes ALL of: **the value of the `count` field from the tool output (copy the digit verbatim — do not re-count the `matches` array yourself and do not recall the number from earlier context such as the user's message, prior assistant turns, or the Teams nudge text)**, the fact that the suggestions appear below this message, and that clicking a role opens its full details on the right side panel. Compose your own phrasing — do NOT copy a stock template verbatim."*

**Edit 3 (new subsection — insert right after the `suggest_requisitions` response template, before the `view_requisition` bullet at line 94).**

Insert this new bullet/subsection at the same indent level as the `view_requisition` bullet:

```markdown
- **Answering "are there any more?" / "is that all?" / "show me more":** Reference `count` and `size` from your most recent `suggest_requisitions` result in conversation history. Do NOT call any tool to answer this question; do NOT derive the answer from the Teams nudge text or the user's prior assertions.
    - `count == size` (the previous page was full) → reply briefly: "There may be more — want me to load the next set?" Then call `emit_followups` with `scenario_id: matches_returned` and `pills: ["Show more", "Ask about a role"]`. Wait for the user's confirmation before issuing another `suggest_requisitions` call (see Tool Trigger Rule 1).
    - `count < size` (the previous page was not full) → reply: "Those are all the role suggestions available right now — `<count>` in total." Then call `emit_followups` with `scenario_id: matches_returned` and `pills: ["Update my skills", "Ask about a role"]`.
```

#### 6.2.5 Tests

Add to `RequisitionMatchingToolsTest.java`:

1. **`suggestRequisitions_populatesCountSizePage_fullPage()`** — wire a stub `requisitionMatchApi` that returns a `PaginatedResource` with 5 items, request `page=0`, `size=5`; assert returned `SuggestRequisitionsOutput` has `count == 5`, `size == 5`, `page == 0`.
2. **`suggestRequisitions_populatesCountSizePage_partialPage()`** — stub returns 4 items, request `page=0`, `size=5`; assert `count == 4`, `size == 5`, `page == 0`.
3. **`suggestRequisitions_defaultsSizeAndPageWhenInputNull()`** — call with `SuggestRequisitionsInput(null, null)`, stub returns 3 items; assert `page == 0`, `size == 5` in output (defaults from the tool).
4. **`suggestRequisitions_emitsRequiredNextActionForNoMatches()` / `forMatches()`** — unchanged behavior, but re-check both branches still set `requiredNextAction` correctly.

#### 6.2.6 Risks / trade-offs

- **`count == size` on the last page produces a false-positive "load more" offer.** If exactly 5 matches survive the score filter and there is no page 1, the agent will offer "Want me to load the next set?". User accepts; next call returns 0; the no-matches template fires ("No role suggestions found — want to update your skills?"). The user sees one extra round-trip. Acceptable.
- **No visibility into the score-filter silent drop.** The agent will say "Those are all 4 role suggestions" even though Azure Search returned 5 and one was filtered. Same as today; no regression. Surfacing the filter is out of scope here — would require new fields and prompt language that may leak internal scoring concepts.
- **Tool output is now subtly more chatty.** Two integers and one more added to every response. Negligible token cost (~5 tokens per call).

---

### 6.3 Approach B — Full pagination semantics (domain + BFF + prompt)

Approach B keeps every change from Approach A and adds two pieces: (1) the domain service computes the **true post-filter total**, and (2) the tool surfaces it as `totalAvailable` plus a derived `hasMore` boolean. The prompt then references `hasMore` directly instead of relying on the `count == size` heuristic.

#### 6.3.1 Files changed

| Path | Change |
|---|---|
| `domain-service/requisition-service/src/main/java/com/example/svc/service/RequisitionMatchService.java` | Fetch a fixed pool, filter once, page in-memory; return `PaginatedResource` with filtered `totalElements` |
| `api-service/bff-service/src/main/java/com/example/svc/domain/tools/requisitionmatching/SuggestRequisitionsOutput.java` | Add `totalAvailable` and `hasMore` on top of A's fields |
| `api-service/bff-service/src/main/java/com/example/svc/ai/tool/RequisitionMatchingTools.java` | Read `totalElements` and `hasNext` from the `PaginatedResource` and forward |
| `api-service/bff-service/src/main/resources/agents/local/RequisitionMatchingAgent.md` | 3 prompt edits — same shape as A but reference `hasMore`/`totalAvailable` |
| `domain-service/requisition-service/src/test/java/com/example/svc/service/RequisitionMatchServiceTest.java` | New tests for filtered total + correct `hasNext` |
| `api-service/bff-service/src/test/java/com/example/svc/ai/tool/RequisitionMatchingToolsTest.java` | Assertions for `totalAvailable` and `hasMore` |

Verify (no change expected): `adapter-service/hr-data-mesh-batch-service/src/test/java/com/example/svc/batch/processor/TalentProfileToSuggestedOpenRolesNudgeEventProcessorTest.java` — the processor reads only `getContent()`, never `getTotalElements()` / `isHasNext()`, so its behavior is unaffected.

#### 6.3.2 `RequisitionMatchService.java` — full method rewrite

**Context.** Today the method at `:127-171` calls Azure Search once with `top = input.getSize()`, filters by `isGoodSuggestion`, and constructs `PaginatedResource(filteredPage, searchResults.getTotalCount(), input.getPage(), input.getSize())`. Three problems:

1. `searchResults.getTotalCount()` is the raw Azure count (pre-filter), so `totalElements` is overstated.
2. Azure only returns one page; we can't know whether the *next* page would yield more good matches.
3. `setTop(input.getSize())` + `setSkip(page * size)` couples Azure pagination to caller pagination, which means filter drops in early pages produce holes in later pages.

**Fix.** Always fetch a fixed-size candidate pool, apply `isGoodSuggestion` once, then slice the filtered list to the caller's requested page. Add a constant `MATCH_POOL_SIZE` for the pool ceiling.

Insert the constant near the existing constants (`:96` area):

```java
/**
 * Upper bound on candidates we evaluate per match request. Azure Search is asked for this many
 * results, the score-based {@link #isGoodSuggestion} filter is applied once, and the resulting
 * list is then paged in-memory. Sized comfortably above the typical strong-match count (~10)
 * so that {@code totalElements} and {@code hasNext} on the returned {@link PaginatedResource}
 * are accurate for realistic page-1+ requests.
 */
static final int MATCH_POOL_SIZE = 50;
```

Rewrite `match(...)` in full. **Before** (`:127-171`):

```java
@Override
public PaginatedResource<RequisitionMatchDto> match(RequisitionMatchRequest input) {
    log.info("Debiasing requisition match request.");
    debias(input);
    log.info("Debiased requisition match request.");

    String searchQuery = createSearchQuery(input);
    SearchClause searchClause = new RequisitionHybridSearchClause(searchQuery);

    SearchOptions searchOptions = buildSearchOptionsForSearch(input, searchClause);

    log.info("Performing search.");
    SearchPagedIterable searchResults = searchClient.search(searchClause.query(), searchOptions, Context.NONE);

    List<RequisitionSearchResult> requisitionResults = searchResults.stream()
            .map(searchResult -> requisitionMapper.mapToRequisitionSearchResult(
                    searchResult, searchResult.getDocument(Requisition.class)))
            .toList();
    log.info("Retrieved {} search results.", requisitionResults.size());

    List<RequisitionMatchDto> requisitionMatches = requisitionResults.stream()
            .peek(match -> log.info(
                    "Role suggestion {} with external ID {} has score {}.",
                    match.getRequisition().getSearchId(),
                    match.getRequisition().getExternalSourceId(),
                    match.getRerankerScore()))
            .filter(RequisitionMatchService::isGoodSuggestion)
            .map(result -> {
                if (input.isIncludeExplanation()) {
                    return getRoleSuggestionWithExplanation(result, input);
                } else {
                    return RequisitionMatchDto.builder()
                            .matchScore(calculateMatchScore(result.getRerankerScore()))
                            .talentProfile(requisitionMapper.mapToTalentProfileDto(input.getTalentProfile()))
                            .requisition(requisitionMapper.mapToSearchableRequisitionDto(result.getRequisition()))
                            .rerankerScore(result.getRerankerScore())
                            .build();
                }
            })
            .filter(Objects::nonNull)
            .collect(Collectors.toList());

    return new PaginatedResource<>(
            requisitionMatches, searchResults.getTotalCount(), input.getPage(), input.getSize());
}
```

**After:**

```java
@Override
public PaginatedResource<RequisitionMatchDto> match(RequisitionMatchRequest input) {
    log.info("Debiasing requisition match request.");
    debias(input);
    log.info("Debiased requisition match request.");

    String searchQuery = createSearchQuery(input);
    SearchClause searchClause = new RequisitionHybridSearchClause(searchQuery);

    // Fetch the full candidate pool so we can apply isGoodSuggestion once and page deterministically.
    // The caller's page/size only governs how the post-filter list is sliced below.
    SearchOptions searchOptions = buildSearchOptionsForSearch(input, searchClause);
    searchOptions.setTop(MATCH_POOL_SIZE);
    searchOptions.setSkip(0);

    log.info("Performing search.");
    SearchPagedIterable searchResults = searchClient.search(searchClause.query(), searchOptions, Context.NONE);

    List<RequisitionSearchResult> requisitionResults = searchResults.stream()
            .map(searchResult -> requisitionMapper.mapToRequisitionSearchResult(
                    searchResult, searchResult.getDocument(Requisition.class)))
            .toList();
    log.info(
            "Retrieved {} search results (pool size {}).",
            requisitionResults.size(),
            MATCH_POOL_SIZE);

    List<RequisitionMatchDto> filteredAll = requisitionResults.stream()
            .peek(match -> log.info(
                    "Role suggestion {} with external ID {} has score {}.",
                    match.getRequisition().getSearchId(),
                    match.getRequisition().getExternalSourceId(),
                    match.getRerankerScore()))
            .filter(RequisitionMatchService::isGoodSuggestion)
            .map(result -> {
                if (input.isIncludeExplanation()) {
                    return getRoleSuggestionWithExplanation(result, input);
                } else {
                    return RequisitionMatchDto.builder()
                            .matchScore(calculateMatchScore(result.getRerankerScore()))
                            .talentProfile(requisitionMapper.mapToTalentProfileDto(input.getTalentProfile()))
                            .requisition(requisitionMapper.mapToSearchableRequisitionDto(result.getRequisition()))
                            .rerankerScore(result.getRerankerScore())
                            .build();
                }
            })
            .filter(Objects::nonNull)
            .collect(Collectors.toList());

    int page = input.getPage();
    int size = input.getSize();
    int from = Math.min(page * size, filteredAll.size());
    int to = Math.min(from + size, filteredAll.size());
    List<RequisitionMatchDto> pageSlice = filteredAll.subList(from, to);

    log.info(
            "Returning page {} (size {}) — {} of {} filtered matches.",
            page,
            size,
            pageSlice.size(),
            filteredAll.size());

    return new PaginatedResource<>(
            pageSlice, (long) filteredAll.size(), page, size);
}
```

`PaginatedResource`'s `(List<T> content, long totalElements, int pageNumber, int pageSize)` constructor (see `common-library/.../PaginatedResource.java`) computes `totalPages`, `hasNext`, and `isLast` from these inputs, so no other field-setting is required.

#### 6.3.3 `SuggestRequisitionsOutput.java` — full new content (extends A)

Replace the file's contents with:

```java
package com.example.svc.domain.tools.requisitionmatching;

import java.util.List;

/**
 * Output of the {@code suggest_requisitions} tool.
 *
 * <p>The {@code count}, {@code totalAvailable}, {@code hasMore}, {@code size}, and {@code page}
 * fields exist specifically so the agent prompt can reference them by name when composing the
 * user-facing response. The LLM never counts the {@code matches} array itself, and never derives
 * pagination state from prior conversation context — both numbers come straight from the tool.
 *
 * @param matches            current page of role suggestions
 * @param count              {@code matches.size()} — the digit the LLM must echo verbatim
 * @param totalAvailable     total post-filter matches across all pages (from the domain service's filtered total)
 * @param hasMore            {@code true} iff there is at least one more page after this one
 * @param size               page size used for this call
 * @param page               zero-based index of the current page
 * @param requiredNextAction inline directive for the next required tool call
 */
public record SuggestRequisitionsOutput(
        List<RequisitionMatch> matches,
        int count,
        long totalAvailable,
        boolean hasMore,
        int size,
        int page,
        String requiredNextAction) {}
```

#### 6.3.4 `RequisitionMatchingTools.java` — diff

Same shape as Approach A but reads `totalElements` and `hasNext` from the `PaginatedResource`. Replace the same `:130-155` block:

```java
        List<RequisitionMatchDto> matchDtos = toReturn.getContent();
        Map<String, TalentProfileDto> recruiterProfileMap = resolveRecruiterProfiles(matchDtos.stream()
                .map(dto -> dto.getRequisition() != null ? dto.getRequisition().getRecruiter() : null)
                .toList());

        List<RequisitionMatch> matches = matchDtos.stream()
                .map(dto -> {
                    String recruiterId =
                            dto.getRequisition() != null ? dto.getRequisition().getRecruiter() : null;
                    TalentProfileDto recruiterProfile =
                            recruiterId != null ? recruiterProfileMap.get(recruiterId) : null;
                    ViewRequisitionOutput req =
                            toolMapper.mapToViewRequisitionOutput(dto.getRequisition(), recruiterProfile);
                    return toolMapper.mapToRequisitionMatch(dto, req);
                })
                .toList();

        int page = input.page() == null ? 0 : input.page();
        int size = input.size() == null ? 5 : input.size();
        int count = matches.size();
        long totalAvailable = toReturn.getTotalElements() == null ? count : toReturn.getTotalElements();
        boolean hasMore = toReturn.isHasNext();

        // (requiredNextAction block unchanged — keep as in Approach A)
        String requiredNextAction = matches.isEmpty()
                ? "REQUIRED next action: call emit_followups with scenario_id=\"no_matches\" and pills=[\"Update my skills\", \"Show profile gaps\"] before ending the turn. Do not skip — this is the last action of your turn."
                : "REQUIRED next action: call emit_followups with scenario_id=\"matches_returned\" and pills=[\"Show more\", \"Ask about a role\"] before ending the turn. Do not skip — this is the last action of your turn.";

        return new SuggestRequisitionsOutput(
                matches, count, totalAvailable, hasMore, size, page, requiredNextAction);
```

#### 6.3.5 `RequisitionMatchingAgent.md` — three prompt edits (extends A's wording)

**Edit 1 (line 71 — pagination clause).** Same as Approach A's Edit 1, except the trigger condition references `hasMore` instead of `count == size`:

> *"The tool currently accepts `page` (default 0) and `size` (default 5). **Only call `suggest_requisitions` again with `page = previous_page + 1` if the previous tool result had `hasMore == true`. If `hasMore == false`, do NOT call the tool again — the user has already seen everything available. Reuse the previous result.** Do not fabricate filters — the tool does NOT accept location, level, skills, department, country, or free-text search parameters today. If the user asks for a filter you cannot apply (e.g. "only roles in London"), acknowledge honestly that filtering is not yet supported from chat and still return the current matches."*

**Edit 2 (line 85 — intro composition).** Identical to Approach A's Edit 2. The `count` field is the authoritative source for the prose count under both approaches.

**Edit 3 (new "Answering 'are there any more?'" subsection).** Same insertion point as Approach A, with the conditions referencing `hasMore` and `totalAvailable`:

```markdown
- **Answering "are there any more?" / "is that all?" / "show me more":** Reference `hasMore`, `count`, and `totalAvailable` from your most recent `suggest_requisitions` result in conversation history. Do NOT call any tool to answer this question; do NOT derive the answer from the Teams nudge text or the user's prior assertions.
    - `hasMore == true` → reply briefly: "There are more — you've seen `<count>` of `<totalAvailable>`. Want me to load the next set?" Then call `emit_followups` with `scenario_id: matches_returned` and `pills: ["Show more", "Ask about a role"]`. Wait for the user's confirmation before issuing another `suggest_requisitions` call (see Tool Trigger Rule 1).
    - `hasMore == false` → reply: "Those are all the role suggestions available right now — `<totalAvailable>` in total." Then call `emit_followups` with `scenario_id: matches_returned` and `pills: ["Update my skills", "Ask about a role"]`.
```

#### 6.3.6 Tests

**`RequisitionMatchServiceTest.java`** — add three cases:

1. **`match_filtersBelowThresholdAndReportsFilteredTotal()`** — stub `searchClient.search` to return 10 candidates: 7 with reranker score `> 1.8` and 3 with score `≤ 1.8`. Call with `page=0`, `size=5`. Assert returned `PaginatedResource` has `content.size() == 5`, `totalElements == 7L`, `pageNumber == 0`, `pageSize == 5`, `hasNext == true`.
2. **`match_returnsTailPageWhenRequested()`** — same stub. Call with `page=1`, `size=5`. Assert `content.size() == 2`, `totalElements == 7L`, `pageNumber == 1`, `pageSize == 5`, `hasNext == false`.
3. **`match_fetchesPoolSizeFromAzure()`** — `ArgumentCaptor<SearchOptions>`; verify that the `SearchOptions` passed to `searchClient.search` has `getTop() == 50` and `getSkip() == 0`, regardless of caller's `page`/`size`.

**`RequisitionMatchingToolsTest.java`** — add (in addition to Approach A's tests):

1. **`suggestRequisitions_propagatesHasMoreFromPaginatedResource()`** — stub `requisitionMatchApi` to return a `PaginatedResource` with `hasNext == true`, `totalElements == 17`. Assert returned output has `hasMore == true`, `totalAvailable == 17L`.
2. **`suggestRequisitions_propagatesNoMoreWhenLastPage()`** — `hasNext == false`. Assert `hasMore == false`.
3. **`suggestRequisitions_defaultsTotalAvailableToCountWhenTotalElementsNull()`** — defensive: if `getTotalElements()` returns `null` (legacy callers), `totalAvailable` falls back to `count`.

#### 6.3.7 Risks / trade-offs

- **`MATCH_POOL_SIZE = 50` is a magic constant.** With reranker threshold `1.8`, real-world strong-match counts are typically <20; 50 is comfortably wide. If a user ever has >50 strong matches, page 11+ (with default `size=5`) will be empty regardless of how many strong matches actually exist. Acceptable today; revisit if telemetry shows users hitting `page=10` cleanly. Make it a `@ConfigurationProperties` field instead of a constant if config-tunability is wanted later.
- **One additional Azure call cost** per `match()` request when caller would previously have asked for `size=5` (now we always ask for 50). Empirically this is one Azure request either way (Azure Search returns all results in a single response under `top=50`); the cost difference is in egress bandwidth, ~10× more documents serialized. Negligible.
- **`PaginatedResource.totalElements` semantics changes** for every caller, not just the chat agent. Audited callers: `RequisitionMatchingTools` (chat agent, the target of this fix) and `TalentProfileToSuggestedOpenRolesNudgeEventProcessor` (`adapter-service/.../processor/TalentProfileToSuggestedOpenRolesNudgeEventProcessor.java:67-77`). The processor reads only `response.getContent()` — it does not consult `totalElements` or `hasNext`. So no behavior change there. Confirm with a focused test on the processor before shipping.
- **GraphQL caller** (`RequisitionMatchResolver` if present in `api-service/bff-service/graphql`): grep `requisitionMatchApi.match(` across `api-service/` to enumerate all callers and confirm none reads `totalElements`. If any does, decide whether the new semantics are acceptable or whether the GraphQL layer needs to translate.
- **In-memory paging.** `filteredAll.subList(...)` returns a view backed by `filteredAll`. The returned `PaginatedResource.content` references that view. Avoid mutating `filteredAll` after construction (we do not today). If paranoia is warranted, wrap with `new ArrayList<>(filteredAll.subList(...))`.
- **Explanation-LLM null-drop interaction.** The existing FIXME at `:213-215` silently drops a role from the filtered list when its explanation LLM call returns `null`. With Approach B, that drop now correctly reduces `totalElements`, so `hasMore` and `totalAvailable` remain consistent. The drop itself is still a separate concern tracked under `&lt;internal-issue-ref&gt;`.

---

### 6.4 Recommended path

For the immediate bug (count hallucination + wrong "are there any more?"): **Approach A** is sufficient. Three files, one test class touched, no domain-service blast radius. Ship and validate against the reporter's session.

If the team also wants the "Show more" UX to be deterministic and the prose to support "Showing 4 of 17" framing: layer **Approach B** on top. B is additive — every field A introduces stays in place; B just adds `totalAvailable` and `hasMore`. The prompt edits between A and B differ only in the condition (`count == size` vs `hasMore`), so prompt evolution is a one-line swap.

Neither approach addresses the nudge-side time drift (the deeper question of "why are these 4 different roles than the 5 the nudge showed?"). That is documented in §6.5 below as a separate, longer-horizon fix.

### 6.5 Out-of-scope but related (deferred)

- **Plumb the nudge ID into the chat-agent deep link** so the agent can read `SuggestedOpenRolesNudgeEntity.matchIds` on entry and present the nudged-set first, with a clearly-labelled "fresh suggestions" set second. Fixes the nudge-vs-agent identity divergence (§2a) structurally. Touches the Teams card builder, the SSE entry, and the agent's first-message handler. Material change; out of scope here.
- **Mark the Teams nudge card with "as of <timestamp>"** so the user expects drift. Pure UX change in `m365-teams-bot-service/.../suggestedOpenRolesCardBuilder.ts`. Cheapest if the deep-link plumbing in the previous bullet is too invasive.

---

## Appendix — File / line references used in this doc

- `adapter-service/hr-data-mesh-batch-service/src/main/java/com/example/svc/batch/processor/TalentProfileToSuggestedOpenRolesNudgeEventProcessor.java` — nudge batch processor; `includeExplanation(false)`, no per-role filtering of its own.
- `adapter-service/m365-teams-bot-service/src/services/builders/suggestedOpenRolesCardBuilder.ts:428` — Teams card deep link (no IDs).
- `api-service/bff-service/src/main/java/com/example/svc/ai/tool/RequisitionMatchingTools.java:90-156` — agent-facing tool; default `includeExplanation` resolves to `true`; logs final count at `:125-128`.
- `api-service/bff-service/src/main/java/com/example/svc/controller/ai/AgentController.java:34-50` — SSE entry; no nudge context.
- `api-service/bff-service/src/main/java/com/example/svc/domain/data/nudge/SuggestedOpenRolesNudgeEntity.java:28-34` — Cosmos persistence of `matchIds`, `newMatchIds`.
- `api-service/bff-service/src/main/resources/agents/local/RequisitionMatchingAgent.md:85, 102, 141-142` — count is free-form; no redundant tool calls; no first-message nudge pre-seed.
- `domain-service/requisition-service/src/main/java/com/example/svc/service/RequisitionMatchService.java:96` — `MINIMUM_RERANKER_SCORE = 1.8`.
- `domain-service/requisition-service/src/main/java/com/example/svc/service/RequisitionMatchService.java:148-152, 178-183, 213-215` — score-filter log lines and the explanation-null silent drop.
- `domain-service/requisition-api/src/main/java/com/example/svc/api/domain/input/match/RequisitionMatchRequest.java:27` — `includeExplanation` defaults to `true`.
