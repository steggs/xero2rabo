"""Ouen Accounting runner, phase one.

A read-only pipe between Karbon and an auditable local store, plus the stall
check that sits on top of it. There are no department agents in this package
and nothing here can send or file anything. See runner/policy.py for the code
that enforces that rather than describing it.
"""

__version__ = "0.1.0"
