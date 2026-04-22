"""app.importers — hooks for batch ingestion into the search backend.

Each importer yields ``dict`` documents that the active
:class:`~app.adapters.base.BackendAdapter`'s ``bulk_index()`` then
ships to the search engine. Keeping importers dict-shaped rather
than ``Record``-shaped leaves every backend-specific pre-processing
(timestamp normalisation, nested-field flattening, …) at the
adapter boundary.

Sprint 22 ships the first importer (OAI-PMH / Dublin Core).
Sprint 23-26 add LIDO, MARC/UNIMARC, CSV/XLSX, EAD.
"""
