<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Release Description Template

Copy this template into the GitHub Release description, then remove unused sections and all comments. Keep the body focused on what users need to understand, install, upgrade, and verify this release. Link user-visible changes to their pull requests or issues when a link adds useful context.

```markdown
## Highlights

- [User-visible capability or important behavior change.] ([#PR](URL))
- [Important reliability, performance, platform, or documentation improvement.] ([#PR](URL))

## Changes

### New Features

- [What is available and who can use it.] ([#PR](URL))

### Improvements

- [Behavior, performance, observability, deployment, or developer experience improvement.] ([#PR](URL))

### Fixes

- [User-visible bug and the behavior after the fix.] ([#PR](URL))

### Documentation

- [New or corrected user or maintainer documentation.] ([#PR](URL))

## Compatibility

| Area | This release | Notes |
| --- | --- | --- |
| Python | [version range] | [runtime requirement or packaging note] |
| Kubernetes | [supported range] | [deployment limitation, if relevant] |
| NVIDIA | [supported runtime/image] | [compatibility or image note] |
| MetaX | [supported runtime/image] | [compatibility or image note] |
| API and configuration | [compatible / changed] | [field, endpoint, or protocol note] |

## Breaking Changes and Deprecations

- [Removed, renamed, or behavior-changing interface.] Use [replacement or migration action].

## Upgrade Notes

1. [Required version, image, Chart, CRD, or configuration update.]
2. [Command or migration action, if required.]
3. [Default behavior or rollback consideration, if it changes the operator's action.]

## Release Artifacts

| Artifact | Version or tag | Install or access path |
| --- | --- | --- |
| Python package | `foretoken==X.Y.Z` | `pip install foretoken==X.Y.Z` |
| OCI images | `X.Y.Z` | `ghcr.io/shiweijiezero/foretoken/<name>:X.Y.Z` |
| MetaX model-server | `X.Y.Z-metax` | `ghcr.io/shiweijiezero/foretoken/model-server:X.Y.Z-metax` |
| Helm Chart | `X.Y.Z` | `oci://ghcr.io/shiweijiezero/foretoken/charts` |
| Examples | `vX.Y.Z` | [examples at the release tag](https://github.com/shiweijiezero/foretoken/tree/vX.Y.Z/examples) |

## Known Limitations

- [Current limitation that changes whether or how users should install, upgrade, or operate this release.]

## Thanks

- [@contributor](URL) — [specific code, test, documentation, issue, hardware, or other support].
- Thanks to everyone who reported issues, reviewed changes, tested releases, or helped improve Foretoken.

## Full Changelog

[Compare `vPREVIOUS` to `vX.Y.Z`](https://github.com/shiweijiezero/foretoken/compare/vPREVIOUS...vX.Y.Z)
```

## Authoring rules

- Use `Highlights` for three to five outcomes, not a commit list.
- Keep `Changes` grouped by user-facing area. Combine related pull requests into one explanation.
- Keep `Compatibility` and `Upgrade Notes` when a reader may need to choose a runtime, change configuration, or take an action before using the release.
- List the package, each published OCI image variant, the Chart, and the tagged examples when they are part of the release.
- Keep `Known Limitations` short and actionable. Omit it when there is nothing that changes user action.
- Name contributors only from confirmed contribution or support records, and describe the concrete help provided.
- Put the complete commit history in the compare link rather than expanding every internal commit in the release body.
