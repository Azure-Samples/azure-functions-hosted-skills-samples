"""Stage 1 of the alert pipeline: deterministic rules and Microsoft-Decision-1 routing.

The Event Hubs function in ``function_app.py`` calls :func:`router.route_batch` for every
batch of alerts. Nothing in this package calls a generative model; only alerts that need
an analyst-style investigation reach the hosted skill in ``alert_investigator.agent.md``.
"""
