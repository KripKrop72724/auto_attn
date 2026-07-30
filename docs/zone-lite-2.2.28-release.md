# Zone Lite 2.2.28 corrective release

Zone Lite 2.2.28 completes the bounded catalog activation introduced in
2.2.27. The SWAT canary proved that ESP-IDF's mounted storage VFS can support
`rename()` while not reliably supporting `access()`. That caused the atomic
activation code to misclassify an existing catalog as absent and reject the
final rename.

The activation path now uses the result of renaming the active catalog to its
backup as the authoritative existence check:

- successful backup rename means an active catalog existed;
- `ENOENT` means this is the first catalog;
- every other error fails closed;
- a failed staged activation restores the backup;
- a successful activation removes the backup.

Rollout remains SWAT-only until boot health, catalog generation, truth
delegation, attendance preservation, and Oracle delivery are all proven.
