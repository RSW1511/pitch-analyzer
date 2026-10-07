"""Patriot Pricing Engine.

Deterministic pricing core for services-contract recompetes. Implements the
modules in Game Plan v2 section 5: intake (M0), Step 1 collectors (M1),
extractors (M2), differ (M3), assumptions register (M4), pricing core (M5),
checks (M6), question generator (M7), exporter (M8) and second brain (M9).

Nothing in the price path is a language model. An AI agent may draft
``assumptions.csv``; a person approves it; this package does every calculation.
"""

__version__ = "0.1.0"
