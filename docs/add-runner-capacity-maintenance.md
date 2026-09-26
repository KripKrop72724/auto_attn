# ADD Windows runner capacity maintenance

The production runner can fill its Windows system drive before `actions/checkout` starts. When this happens, deployment logs may also fail to upload, leaving the existing ADD release healthy but blocking new deployments and protected firmware signing.

Run the `ADD runner capacity maintenance` workflow on `main` with `inspect` first. It uses no checkout or downloaded actions and reports the system drive's free space, size of the Actions cache, temporary directory and repository workspace, and Docker disk usage. If generated artifacts are the cause, run it again with `clean-generated`. That mode removes only the workflow workspace's unsigned firmware and ESP-IDF build directories plus incomplete `_actions/_temp_*` action-download staging directories. It does not delete signed release artifacts, production configuration, PostgreSQL data, Docker images, or volumes.

After capacity is restored, rerun the failed ADD deployment and protected firmware HIL candidate workflow, then verify public health and the exact signed release identity before starting a device campaign.
