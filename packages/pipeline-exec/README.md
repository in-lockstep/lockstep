# pipeline-exec

The deterministic half of a compiled pipeline: fan-out, sharding, coverage verification, schema and
content validation. Generated workflows install this and nothing else — never the compiler.

Its command-line surface is emitted verbatim by [Lockstep](../../README.md), so the two are tested
together: `tests/test_contract.py` in the repo root asserts that every invocation the compiler emits
parses against this CLI.
