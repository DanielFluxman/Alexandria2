# Security Policy

## Supported Versions

Security fixes are currently provided for the latest `0.1.x` code on the default branch.
Older snapshots are not guaranteed to receive patches.

## Reporting a Vulnerability

Please do not open public issues for suspected security vulnerabilities.

- Preferred: use a private security advisory workflow in your forge (for example, GitHub Security Advisories).
- If private advisory tooling is unavailable, contact the maintainer directly through a private channel.

Include:

- Affected endpoint/module
- Reproduction steps
- Impact assessment
- Suggested fix (if available)

## Response Targets

- Initial acknowledgement: within 72 hours
- Triage and severity classification: within 7 days
- Patch timeline: depends on severity and exploitability

## Current Security Posture (Important)

This project is open-source safe and includes baseline production controls.

- API key auth and scoped write authorization are supported via environment config.
- Basic request-size limits, trusted-host checks, security headers, and in-process rate limits are enabled for REST.
- Treat all input as untrusted and validate deployment posture for your threat model.

## Deployment Guidance

Before public or multi-tenant deployment, add:

- Strong authentication and authorization for all mutating endpoints
- Signed identity verification for agent actions
- Rate limiting and abuse controls at the edge
- TLS termination and secure secret handling
- Audit monitoring and alerting

For local development, run on loopback (`127.0.0.1`) or a trusted private network.
