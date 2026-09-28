"""Application packets (P6): fill sheet, tailoring, claim-check, rendering.

Everything generated traces back to master_resume/ — the claim-check gate
(claim_check.py) makes fabrication structurally visible, and a packet cannot
reach `ready` with unresolved unsupported claims (PLAN §5.7).
"""
