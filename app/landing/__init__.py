"""Public landing page (Sprint 28).

The wizard, the admin console and the OAI endpoint all live behind
internal URLs. Non-technical operators who open the base URL for the
first time need a page that tells them *what EGG-API is*, *who it's
for* and *how to get started* — without assuming they know FastAPI,
OpenSearch or OAI-PMH. This module ships that page.

The landing page is HTML only, uses a tiny dedicated stylesheet
(:file:`landing.css`) and reuses Jinja2 autoescape. It is intentionally
decoupled from the admin UI templates so the marketing copy never has
to share layout with the operator console.
"""
