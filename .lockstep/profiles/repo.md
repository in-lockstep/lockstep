---
name: repo
description: This repository, checked out by the workflow that runs the gate
# Reaches every agent this repository compiles, which right now is the four inherited review lenses.
# That is what makes them reviewers of *this* codebase rather than of a generic Python project — and
# it is also what makes them agents their upstream never evaluated, which `doctor` will say.
contexts: [codebase]
github:
  deploy:
    mode: external
---
