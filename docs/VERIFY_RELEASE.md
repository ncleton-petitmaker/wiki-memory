# Verify a Wiki Memory release

Verify a release before installing its wheel, loading its catalog, or pinning
its Team image. These commands download only public release artifacts; they do
not require a vault, an OIDC token, or any production credential.

```bash
repo=ncleton-petitmaker/wiki-memory
tag=v1.0.0-alpha.24 # replace with the exact release you intend to use
directory="$(mktemp -d)"

gh release download "$tag" --repo "$repo" --dir "$directory"
cd "$directory"
sha256sum --check SHA256SUMS
```

Install [Cosign](https://docs.sigstore.dev/cosign/system_config/installation/),
then verify both keyless signature bundles. The GitHub Actions certificate is
constrained to this repository’s release workflow and an exact version tag;
the commands do not trust an arbitrary Fulcio certificate with the same key.

```bash
identity="^https://github.com/ncleton-petitmaker/wiki-memory/.github/workflows/release.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$"

cosign verify-blob --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp "$identity" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com SHA256SUMS

cosign verify-blob --bundle plugin-catalog.sigstore.json \
  --certificate-identity-regexp "$identity" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com plugin-catalog.json
```

For Team, pin the image to the signed release digest, never a tag. Verify it
with the same identity constraint:

```bash
cosign verify \
  --certificate-identity-regexp "$identity" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/ncleton-petitmaker/wiki-memory@sha256:REPLACE_WITH_RELEASE_DIGEST
```

The release workflow executes the checksum and both `verify-blob` commands
before publishing the release. A failure means do not install the artifact.
