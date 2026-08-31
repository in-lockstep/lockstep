"""Acme's review prompts. Resources only — this module deliberately holds no code.

A prompt body is data, and `ai/prompt.py` gives the reason: a prompt change proposed by the
improvement loop must be data rather than executable code entering the import graph of the module
that defines every binding. A pack that ships prose should inherit that property.

So there is nothing here but this docstring, and `in-lockstep pack describe acme-review-prompts`
reports `imports: none` because it walked the AST and found nothing else. Adding a `Prompt`
subclass to this file would be legal and would change that answer to `modules` — which is the
whole reason the field is derived on every pack rather than promised by a category.
"""
