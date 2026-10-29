# Repository maintenance

The project namespace is
[`parkerkyle-max/edge-prl-miner`](https://github.com/parkerkyle-max/edge-prl-miner).
Repository operations must use the authenticated project owner or a collaborator
explicitly authorized for this repository. Check the authenticated identity
before creating a repository or pushing; a configured commit name is not proof
of authentication.

Use repository-local commit identity and a GitHub noreply email when desired.
Do not change global Git identity or unrelated repository credentials as part of
maintaining this project. Never store access tokens in a remote URL, tracked file,
issue, chat transcript or mining configuration.

Enable strong two-factor authentication or a passkey, and keep recovery codes
outside the repository. Where a token is required, prefer a short-lived token
limited to this repository and the minimum necessary permissions. Store it in a
credential manager.

## What belongs in the repository

- Reviewed source, offline tests, public examples, dependency notices and docs.
- The deliberately public developer-fee recipient and fee behavior.
- Sanitized test summaries with clear evidence limits.

Private device configurations, actual device receipts, wallet recovery material,
personal mining accounts, SSH credentials, and unlicensed vendor models or
runtime binaries do not belong here. The developer-fee address is a public payout
identifier; it is not a wallet secret. Keep its exception narrow when scanning
source for personal data.

Review source changes, offline tests and the source manifest before publishing.
Do not describe a new build as device-validated based only on historical results
from a different source revision or configuration.

## CI boundaries

GitHub Actions may run the offline unit tests, documentation checks and builds.
It must not execute real mining or connect an NPU mining job to a pool. GitHub's
additional-product terms prohibit cryptomining through Actions, including
self-hosted runners, and through Codespaces. Run any live pool experiment
directly on the operator's own equipment outside these services.

Official references:

- [Token management](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [GitHub additional-product terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)

The project can be transferred to a dedicated organization later. An organization
separates repository permissions and naming; it does not prevent compromise of
an account that owns it or guarantee anonymity.
