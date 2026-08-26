# Operations guide

## Backup

Pause producers and workers, stop the LeaseQueue process, and copy the SQLite database file from the configured data volume. Copying a live database without SQLite's backup API can produce an inconsistent backup.

## Restore

1. Stop the service.
2. Preserve the current database file separately.
3. Replace it with the verified backup using the same owner and permissions.
4. Start the service and confirm `/health/ready`, `/api/stats`, and a test submission.

## Upgrade and rollback

Build or pull an immutable version tag. Back up the data volume, replace the running image, and verify readiness plus a worker claim. To roll back, stop the new container and start the previous version tag against the preserved database. Review release notes before rollback if a future version introduces a schema migration.

## Queue pressure

Watch `leasequeue_oldest_pending_seconds`, the queued count, retry growth, and dead-letter count. Sustained queue age means worker capacity or an upstream dependency is constrained. Increasing worker concurrency is safe for claims, but each worker must keep its lease alive and complete with the matching token.

## Lost workers

LeaseQueue recovers expired running jobs during the next claim transaction. The job is retried immediately unless its attempt budget is exhausted, in which case it enters the dead-letter state.
