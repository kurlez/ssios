# Licenses and dependencies

## This repository

MIT License. The copyright holder is Kent Liu. See `LICENSE` at the repository root.

There are no runtime Python dependencies. `dependencies` in `pyproject.toml` is an empty list. Tests use the standard-library `unittest`. CI installs only Python 3.12 and does not read repository secrets.

## System dependencies

| Component | When it is needed | License |
| --- | --- | --- |
| Python 3.11+ | Always | PSF License |
| Git | Memory-repository commits | GPL-2.0. Invoked as a separate process. Git source is not linked into this repository |
| Ollama | Only when the provider is changed to `ollama` | See the Ollama project's own license. This repository does not ship model weights |
| Cloud Hypervisor | Only if a microVM is actually started later | Apache-2.0 and BSD-3-Clause, as stated by that project's repository |
| virtiofsd | Same as Cloud Hypervisor | Apache-2.0 and BSD-3-Clause, as stated by that project's repository |

None of these system components is a vendored copy in this repository.

## Vendored browser code

Served from `/ui/vendor/` on the same origin. Pages load nothing from another site.

| Library | Version | Where | License | Used for |
| --- | --- | --- | --- | --- |
| KaTeX | 0.16.22 | `src/ssios/vendor/katex/` | MIT | Math in chat and Notes |
| Mermaid | 10.9.1 | `src/ssios/vendor/mermaid/mermaid.min.js`, license in `LICENSE` beside it | MIT | Diagrams in Notes |

Mermaid is `dist/mermaid.min.js` from the npm package `mermaid@10.9.1`. The tarball matched the registry's integrity digest `sha512-Mx45Obds5W1UkW1nv/7dHRsbfMM1aOKA2+Pxs/IGHNonygDHwmng8xTHyS9z4KWVi0rbko8gjiBmuwwXQ7tiNA==`. The bundle's SHA-256 is `61b335a46df05a7ce1c98378f60e5f3e77a7fb608a1056997e8a649304a936d6`. It runs with `securityLevel: "strict"`.

## Letta

The Letta MemFS documentation and the Letta Code implementation belong to Letta. The upstream repository is <https://github.com/letta-ai/letta-code>, license Apache-2.0. The optional upstream mode pins `@letta-ai/letta-code` 0.34.45. The npm integrity digest is:

`sha512-IhB4vDuNebew2bKbAGYJ3zROPG6kLD2rPhMHXOSSxjExDFCTLAN74d5/4Sn7K8d3yV8n2mZT5uWVcA4FbN+yJw==`

This repository does not copy Letta Code source, prompts, or models. Installation happens in the state directory or a prefix the user names. It does not enter SSIOS's default dependencies. `NOTICE` records the semantic source. The compatibility library is SSIOS's own Python code under the MIT license. It must not be described as a Letta distribution.
