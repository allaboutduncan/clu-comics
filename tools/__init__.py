"""Standalone operator scripts.

A package only so ``core/`` can import from ``repair_db`` without duplicating
it. The scripts themselves must stay runnable on their own -- see
``tools/repair_db.py``, whose whole value is that it works when the app does
not start.
"""
