<!-- docs/checklists/wave1-live-adversary.md -->

# Wave 1 live adversary checklist

Run once against a disposable throwaway repo with a real Grok login. Not for CI.

## Preconditions

- [ ] Grok CLI installed, authenticated (`grok --version` works; any build)
- [ ] Plugin installed from **git marketplace** (`sfourdrinier/grok-skills`) or local path
- [ ] macOS (Seatbelt) or Linux (Landlock + `bwrap` on PATH) for live modes

## Install path

- [ ] Claude: `/plugin marketplace add sfourdrinier/grok-skills` then
      `/plugin install grok@grok-skills`
- [ ] Codex: `codex plugin marketplace add sfourdrinier/grok-skills` then
      `codex plugin add grok@grok-skills`
- [ ] `/grok:preflight` (or companion preflight) succeeds once

## Preflight cache

- [ ] First live command after preflight logs a cache refresh or hit (stderr)
- [ ] Second live command within ~15 minutes hits the cache (no auth-missing)
- [ ] After installing a different Grok CLI build, preflight cache miss re-verifies

## Grounded adversarial review

- [ ] `/grok:adversarial-review --target .` with a small intentional bug
- [ ] Envelope has `policy.webAccess: true` by default
- [ ] Findings are severity-ranked with concrete attacks
- [ ] Either `citations` is non-empty **or** `warnings` includes
      `grounding-requested-no-sources` (never silently "grounded" with zero sources)
- [ ] `/grok:result --pretty` shows a Sources section when citations exist

## Defaults table

- [ ] `reason` without flags: web **off** (`policy.webAccess: false`)
- [ ] `reason --web`: `policy.webAccess: true`
- [ ] `review` without flags: web off
- [ ] `review --web`: web on
- [ ] `verify` never accepts web

## Dual-lens

- [ ] `/grok:dual-lens` or manual adversarial then review on the same target
- [ ] Operator can compare attack pass vs assess pass

## Fail-closed

- [ ] Secret-shaped material never appears unredacted in the envelope
- [ ] Citation URLs with token-shaped query strings are redacted
