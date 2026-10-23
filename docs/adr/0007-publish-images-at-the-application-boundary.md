# 0007: Publish images at the application boundary

Status: Accepted — deployment-boundary correction requested by the owner.

## Decision

Project Watchkeeper owns its Dockerfiles, container builds, image publication,
and portable Compose contract. Deployment platforms consume published images
through that contract; they do not fetch application source or rebuild its
containers in a host-specific service.

After the complete CI suite succeeds for a push to main, publish the app and
restricted reviewer images to GHCR using the repository's short-lived Actions
token. Pull requests and manual validation do not publish. Registry write
permission is scoped to the publication job. Image metadata identifies the
source repository and exact commit.

Publish commit-specific tags for both images and provide a moving main tag for
synthetic smoke tests. Operators pin both images from the same commit, preferably
by registry digest, and use the Compose configuration from that revision. Image
updates do not deploy or activate any platform. Initial publication targets the
validated Linux amd64 runtime; other architectures require separate verification.

The base Compose file consumes images without build contexts or repository-local
configuration mounts. The reviewer's fixed restrictive configuration ships in
its image. An explicit build overlay remains available for development and CI.
Production overlays continue to require externally provisioned secrets, origins,
and networks. Account/subscription authentication and whole-reviewer isolation
remain unchanged from ADR 0003.

## Consequences

A platform needs Compose and its own runtime configuration, not a Watchkeeper
source build environment. Synthetic acceptance must also work with the Compose
file copied to an otherwise empty directory and locally tagged candidate images.

The first actual publication and anonymous pull cannot be verified before the
publishing change reaches main. GHCR package visibility may need an owner action;
CI publication success alone does not prove anonymous pulls. Those checks remain
explicit activation gates, without connecting private conversation accounts.

Moving tags are conveniences rather than immutable deployment records. Both
image digests and the matching Compose revision belong in a deployment's release
record. Database schema and privacy-journal compatibility still govern rollback.
