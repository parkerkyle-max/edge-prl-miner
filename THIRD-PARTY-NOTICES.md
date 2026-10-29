# Third-party provenance

Original Edge PRL project code is licensed under the root MIT license. Imported
or derived components retain the following provenance and applicable notices.
The Python coordination and NPU adapter code was extracted from the project's
earlier local research implementation. The exact numerical/proof path used:

- Pearl official reference: `pearl-research-labs/pearl`, commit
  `0a17bb345a838147a4c14fc618de1839cbc16c1e`.
- Preparation/protocol reference: `benjamin920102/ascend_prl`, commit
  `12c156c24e7db40aaa903a4e79a258b5dd46a33b`.
- BLAKE3 components with their notices retained in `engine/licenses`.

`native-build/prep/prep-single.c` is the attributed preparation derivative;
`native-build/pins.json` records its original upstream file hash and changes.
The proof wrapper and immutable cache additions in `native-build/proof/src/`
call the pinned official implementation. Their build lock records transitive
dependencies but is not a complete license SBOM. Build outputs are excluded.
See [Native build](docs/NATIVE-BUILD.md) for exact inputs and compiler checks.

Retained notice files:

- `engine/licenses/ASCEND-FORK-LICENSE.txt`: MIT, copyright 2026 arabel1a.
- `engine/licenses/PEARL-OFFICIAL-LICENSE.txt`: Pearl's ISC notice and its
  monorepo's component-license summary. A monorepo-wide summary does not replace
  the license of each dependency actually used in a native build.
- `engine/licenses/BLAKE3-LICENSE_A2`, `BLAKE3-LICENSE_A2LLVM` and
  `BLAKE3-LICENSE_CC0`: upstream BLAKE3 notices retained without alteration.

The developer fee is Edge PRL's own support mechanism, not a claim that an
upstream author charges, endorses or receives this fee. Retaining upstream
copyright and permission notices remains required by the applicable licenses;
retaining Edge PRL's fee is a separate voluntary request.

Upstream URLs:

- https://github.com/pearl-research-labs/pearl
- https://github.com/benjamin920102/ascend_prl

The existence of an upstream public repository is not a license grant for
separate proprietary SDKs. This list is not yet a complete native dependency
SBOM. The source review archive intentionally excludes native binaries and
vendor models.
