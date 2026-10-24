# ADD Windows runner capacity maintenance

The production runner can fill its Windows system drive before `actions/checkout` starts. When this happens, deployment logs may also fail to upload, leaving the existing ADD release healthy but blocking new deployments and protected firmware signing.

Dispatch the existing `Deploy ADD` workflow on `main` with `runner_capacity_mode=inspect` first. This maintenance path skips deployment, checkout and downloaded actions. It reports the system drive's free space, size of the Actions cache, temporary directory and repository workspace, and Docker disk usage. If generated artifacts are the cause, dispatch again with `runner_capacity_mode=clean-generated`. That mode removes only the workflow workspace's unsigned firmware and ESP-IDF build directories plus incomplete `_actions/_temp_*` action-download staging directories. It does not delete signed release artifacts, production configuration, PostgreSQL data, Docker images, or volumes.

After capacity is restored, rerun the failed ADD deployment and protected firmware HIL candidate workflow, then verify public health and the exact signed release identity before starting a device campaign.

If a firmware signer is interrupted after decrypting its vault key, wait until
the signer job is fully stopped, then dispatch `clean-signing-scratch`. This
overwrites and removes only `zone-lite-release-sign-<32 hex>` scratch folders
under the runner workspace. It does not touch the vault, signed release output,
ADD data, Docker images, or volumes. Review its removal count before retrying
the signer.
