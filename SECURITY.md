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
- Memory writes are constrained to the selected root and registered vault paths.
- Social workflows stop on verification and access controls.
- Source material is private by default and is never added to this plugin repository.
- Dependency downloads use official package registries or official project endpoints.
- Portable Node.js archives are verified against Node.js-published SHA-256 checksums.

## Release checks

Every release should pass:

```bash
python -m unittest discover -s tests -v
python scripts/privacy_scan.py .
```

CI also runs Gitleaks and the test matrix on macOS, Linux, and Windows.

## Out of scope

Wiki Memory does not attempt to bypass platform authentication controls, secure a compromised operating system, encrypt the vault, or replace backups. Users remain responsible for device security, disk encryption, Syncthing access controls, and backup retention.
