"""A package, so mypy names these modules `tests.in_lockstep.test_x` and the `tests.*` override
in `pyproject.toml` matches them. Without this file mypy called each module by its basename and
the override matched nothing (#248). pytest does not need it; the checker does."""
