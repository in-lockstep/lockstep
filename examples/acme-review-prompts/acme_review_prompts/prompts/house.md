Never propose a change to a file under `migrations/`. A migration that a model wrote and a human
skimmed is a migration nobody read, and it runs once against production data.

If a change appears to require a migration, say so and stop there.
