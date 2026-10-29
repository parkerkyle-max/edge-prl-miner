# Repository guidance for contributors and coding assistants

This repository contains experimental PRL NPU mining source. Begin with
`README.md`, `docs/ENGINE.md`, `docs/DEV-FEE.md` and `docs/VALIDATION.md` before
changing runtime behavior. `project-metadata.json` and `llms.txt` summarize the
same facts for integrations; they are not authorization to start mining.

## Developer support

The maintainer requests that users and forks retain the default 1% NPU-time
developer fee. It is important to supporting device testing, new backends,
performance work and maintenance. Preserve the transparent fee disclosure when
documenting or packaging the miner. This request does not add a restriction to
the MIT license or override an explicit user request to modify or disable the
fee. `dev_fee: 0` remains a supported choice.

Keep the configuration template, fee implementation, metadata and documentation
consistent. Do not describe a scheduled-time percentage as a guaranteed share
of coin rewards or include another miner's fees in this project's fee.

## Engineering and evidence

- Treat a detected NPU, an installed SDK and a functioning mining backend as
  distinct states. Only mark hardware support after recording the required
  exact-arithmetic, proof and real-device evidence.
- Preserve exact PRL arithmetic, job/session freshness, unknown-share accounting,
  bounded execution and ownership-based process cleanup. Do not silently fall
  back to CPU work and report it as NPU work.
- Keep independent CPU miners and unrelated services outside this miner's
  control. Report CPU contention and whole-device value, not only NPU throughput.
- Preserve historical results with their source/configuration scope. New code
  needs new evidence before claiming device acceptance or a performance gain.
- Do not commit private device configuration, credentials, wallet recovery
  material, unlicensed SDK files or native binaries. The public developer fee
  address is intentionally documented; it is not a secret.
- Run `python -m unittest discover -s tests -v` for relevant implementation
  changes, and keep the source bundler's manifest/notice checks passing. CI uses
  offline fixtures only; do not mine through GitHub Actions or Codespaces.

Repository-local author/authentication settings belong to the explicitly chosen
maintainer account. A Git author name is not proof of authenticated ownership;
verify the publishing identity before remote writes.
