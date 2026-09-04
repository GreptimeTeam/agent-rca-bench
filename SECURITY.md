# Security policy

## Supported versions

This repository publishes a measurement, not a deployed service. Fixes land on
`main`; there are no maintained release branches.

## Reporting a vulnerability

Report privately through
[GitHub security advisories](https://github.com/GreptimeTeam/semantic-rca-bench/security/advisories/new),
or email `security@greptime.com` if you cannot use that form. Do not open a
public issue for a vulnerability.

Include what you found, how to reproduce it, and what an attacker gains. You
will get an acknowledgement within five working days.

## What is in scope

- Credential handling in the runner: environment variables, macOS Keychain
  lookups, and anything that could write a key into a log, an artifact, or the
  published report.
- Leakage of private data into `artifacts/`: provider payloads, reasoning text,
  raw telemetry, endpoints, or machine-local paths.
- Code execution reachable from parsing an artifact, a fixture, or a protocol
  file.
- Supply-chain problems in the pinned dependency set.

## What is out of scope

- The benchmark starts local databases and containers on loopback ports while
  measuring. Exposing those ports to a network is a deployment choice, not a
  defect here.
- The runner sends prompts to third-party model providers by design, under
  explicit per-invocation approval. Provider-side handling of that data is
  governed by the provider's own terms.
- Disagreeing with a published measurement result is not a security issue. Open
  a normal issue; see [CONTRIBUTING.md](CONTRIBUTING.md).
