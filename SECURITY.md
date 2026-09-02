# Security policy

Wiki Memory handles private source material and therefore treats local data boundaries as part of its public API.

## Supported versions

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Older pre-1.0 releases | Best effort |

Upgrade to the latest release before reporting an issue when possible.

## Report a vulnerability privately

Use GitHub's [private security advisory form](https://github.com/ncleton-petitmaker/wiki-memory/security/advisories/new). Do not include real vault contents, cookies, credentials, client information, or unrelated personal data.

Please include the affected version, operating system, impact, a minimal synthetic reproduction, and suggested mitigation when known. Maintainers will assess the report and credit the reporter unless anonymity is requested. No guaranteed response-time SLA is offered.

## Security boundaries

- Browser cookies, passwords, local storage, profiles, and authentication state are never read or copied.
- Runtime dependencies, QMD models, indexes, caches, and logs stay outside memory roots.
- Canonical evidence and the append-only ledger stay in the selected memory root; private scope never uploads to Team.
- Memory writes are constrained to the selected root and registered vault paths.
- Team filters ACLs before returning event/search/blob content and rejects generic privileged review events.
- Solo HTTP is loopback-only; secrets use the OS keychain with an external `0600` fallback.
- Unknown Python plugins are quarantined unless solo developer mode is explicit; Team requires catalog trust or verified signatures.
- Social workflows stop on verification and access controls.
- Source material is private by default and is never added to this plugin repository.
- Dependency downloads use official package registries or official project endpoints.
- Portable Node.js archives are verified against Node.js-published SHA-256 checksums.

## Release checks

Every release should pass:

```bash
python -m unittest discover -s tests -v
python scripts/schema_validate.py
python scripts/plugin_conformance.py
python scripts/privacy_scan.py .
```

CI also runs Gitleaks and the test matrix on macOS, Linux, and Windows.

Stable Team releases additionally require the independent review described in
[External security review package](docs/EXTERNAL_SECURITY_REVIEW.md). The
package defines the test boundaries and the attestation a reviewer must return;
it is not replaced by an internal green test run.

## Out of scope

Wiki Memory does not attempt to bypass platform authentication controls, secure a compromised operating system, encrypt a memory root, or turn synchronization into backup. The in-process Python runtime is not a hostile-code sandbox; untrusted privilege-bearing plugins require a separately enforced executable/OCI host. Operators remain responsible for device encryption, TLS, OIDC policy, PostgreSQL PITR, object retention, and tested restoration.
