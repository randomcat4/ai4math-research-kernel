# Product release migrations

This directory intentionally contains no copied business SQL. The sole release sequence is
`docs/spec/product/migration-manifest.json`; `current.lock` pins its exact bytes. The runtime
loads every SQL fragment through the D00a `ProductMigrationRegistry`, verifies the pinned
identity/order/digest bindings, and delegates transaction execution to the D00a assembler.

The current manifest is `BACKEND_ONLY` and not the R00 final seal. Before R00, append a newly
validated fragment at the end, version the manifest and update the lock together. Never renumber
an installed release position or edit a fragment digest in place.
