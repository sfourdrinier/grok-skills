<!-- docs/checklists/wave1-live-results-2026-07-15.md -->

# Wave 1 live results (2026-07-15)

**Historical snapshot** of a dogfood pass on 2026-07-15. Not a live contract —
see `plugin/wrapper/scripts/groklib/web_defaults.py` and CHANGELOG for current
defaults (notably: `reason` default web is **false** after a later flip).

Throwaway repo under `/tmp/grok-dogfood-*` with intentional `buggy.py` (auth bypass + bare-except divide). Host: macOS. CLI at time of probe: `grok 0.2.101 (5bc4b5dfadcf) [stable]`.

## Outcomes

| Check | Result |
|-------|--------|
| Preflight success | pass |
| Preflight cache write + hit on live mode | pass (`ensure_ready: preflight cache hit`) |
| Adversarial `policy.webAccess` | **true** (default) |
| Severity-ranked findings | pass (critical/high/medium/low structured findings) |
| Citations or no-sources warning | **`grounding-requested-no-sources`** (web on, no Sources/URLs harvested) |
| `/grok:result --pretty` | pass (renders response + warnings) |
| `reason --no-web` → webAccess false | pass |
| `reason` default → webAccess true | pass **at probe time**; **current code: reason default is false** |
| `review` default → webAccess false | pass |
| Dual-lens second pass (review after adversarial) | pass |
| Fail-closed envelope (no raw secrets observed) | pass |

## Bug found and fixed in this pass

Adversarial initially failed with `schema-mismatch` / unsupported keyword
`additionalProperties` when companion attached `schemas/review-output.schema.json`.
Schema reduced to the walker-supported keyword set (`type`, `enum`, `required`,
`properties`, `items` only). Re-run succeeded.

## Notes

- Live web tools were enabled; this run did not produce structured stream sources
  or a parseable Sources block, so the fail-closed grounding warning is correct.
- Pin unchanged: installed CLI already matched `accepted-version.json`.
