# Contributing

Thanks for helping improve `grok-skills`. This project is intentionally small on
dependencies and strict on safety contracts.

## Principles

- The **wrapper** (`plugin/wrapper/scripts/grok_agent.py` + `groklib`) owns all
  safety behavior.
- The **plugin** is a thin harness surface: locate the wrapper, pass argv safely,
  relay the envelope verbatim. Do not add safety logic in the plugin.
- Prefer **fail-closed** behavior over silent defaults.
- Keep the wrapper **stdlib-only** (Python) and the plugin scripts **stdlib-only**
  (Node). No new runtime package dependencies without a strong reason.
- Every mode emits **exactly one** JSON result envelope on stdout.
- Keep the wrapper **bundled under** `plugin/wrapper/` so Claude/Codex marketplace
  installs remain self-contained.

## Development setup

Prerequisites:

- Python 3
- Node.js (for plugin unit tests)
- Optional: Claude Code CLI (`claude`) and Codex CLI (`codex`) for install validation
- Optional: authenticated Grok CLI matching `plugin/wrapper/accepted-version.json`

No `pip install` or `npm install` is required for unit tests.

## Tests

### Wrapper (Python)

```bash
cd plugin/wrapper/scripts
python3 -m unittest discover -s tests -q
```

### Plugin (Node)

```bash
cd plugin/scripts
node --test tests/*.test.mjs
```

### Claude plugin validate

```bash
claude plugin validate ./plugin --strict
claude plugin validate .
```

### Live probes (optional, authenticated)

See `plugin/wrapper/scripts/tests/live/README.md`.

### Install smoke (optional)

```bash
export CLAUDE_PLUGIN_ROOT="$PWD/plugin"
node "$CLAUDE_PLUGIN_ROOT/scripts/grok-companion.mjs" preflight
```

## Revalidating the Grok CLI version pin

**Hygiene rule:** the pin is a security boundary. A new Grok CLI build is not
trusted until re-probed. Never bump `accepted-version.json` because
`grok --version` changed on your laptop alone.

Procedure (detail in `plugin/wrapper/references/cli-reference.md`):

1. Install the candidate Grok CLI build.
2. Confirm `grok --version` first line exactly matches what you intend to pin.
3. Run the documented probe / revalidate suite against that build
   (`plugin/wrapper/scripts/tests/live/` and cli-reference revalidation).
4. Only then rewrite `plugin/wrapper/accepted-version.json` (`version`,
   `validatedAtUtc`, `probeEvidence`).
5. Run wrapper unit tests + a live `preflight` from a clean plugin install.
6. Invalidate preflight cache (or wait for TTL): version mismatch must re-probe.
7. Document the pin bump in `CHANGELOG.md` and `docs/COMPATIBILITY.md` if host
   versions were re-checked.

Never hand-edit the pin without re-probing sandbox and auth behavior.

## Code style

- Prefer self-documenting names over long comments.
- Public identity strings: state dir `grok-skills`, owner `grok-skills-wrapper`,
  sandbox profiles `grok-skills-<mode>`, temp homes `gs-`.
- Do not introduce private product names or personal absolute paths into fixtures
  or docs. Use placeholders such as `<operator-home>` and `<tmpdir>`.

## Pull requests

- Include a clear description of behavior change and security impact.
- Add or update unit tests for any contract change.
- Do not weaken fail-closed checks without an explicit security discussion.

## License

By contributing, you agree your contributions are licensed under the MIT License
(see [LICENSE](LICENSE)).
