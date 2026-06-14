# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through GitHub's [private vulnerability reporting][gh-pvr]:
the repository's **Security** tab → **Report a vulnerability**.

Include the affected version, a description of the issue, and steps to
reproduce. You can expect an initial acknowledgement within a few days.

[gh-pvr]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability

## Supported versions

pf-core is pre-1.0 and under active development. Security fixes land in the
latest tagged release; there is no long-term-support branch yet. Pin to a
tagged release and upgrade promptly when a fix ships.

## Scope

pf-core is a framework library, not a deployed service. The most
security-relevant surfaces are:

- **`pf_core.clients`** — the LLM transport layer (HTTP requests, subprocess
  invocation, API-key handling).
- **`pf_core.web`** — the FastAPI app factory and its error-page and
  rate-limit middleware.
- **`pf_core.db`** — the database/connection layer.

API keys and other secrets are read from the environment and must never be
committed. pf-core does not log credential values.
