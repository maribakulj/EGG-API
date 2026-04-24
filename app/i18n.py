"""Minimal i18n (Sprint 29 + Sprint 30).

The target audience of EGG-API (Koha, PMB, AtoM, Mnesys, Ligeo
deployments) includes a large francophone user base — we need the
landing page and the admin UI to speak French without pulling a
full Babel / gettext toolchain.

This module ships a tiny in-memory catalogue with two languages
(``en`` default, ``fr``) and a resolver that inspects, in order:

1. ``?lang=`` query parameter (operator intent, always wins);
2. ``egg_lang`` cookie (remembered preference);
3. ``Accept-Language`` request header (browser defaults);
4. ``AppConfig.default_language`` (Sprint 30: deployment-wide pick
   chosen once by the admin in the setup wizard or config screen);
5. ``EGG_DEFAULT_LANG`` environment variable (ops preset);
6. ``en`` as a last resort.

Strings are accessed by key via :func:`translator` which returns a
callable the Jinja templates (or route handlers) can invoke as
``t("landing.hero.title")``. Unknown keys fall back to English and,
if still missing, return the key itself so the page never crashes on
a typo.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from starlette.requests import Request

logger = logging.getLogger("egg.i18n")


DEFAULT_LANG = "en"
SUPPORTED_LANGS: tuple[str, ...] = ("en", "fr")
LANG_COOKIE = "egg_lang"


# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------

# Kept as plain dicts so operators without Python experience can read the
# file and spot missing translations. Key naming convention:
# ``<surface>.<section>.<role>`` (surface ∈ landing / admin / errors).
EN: dict[str, str] = {
    # Landing — hero
    "landing.hero.title": "The heritage API for institutions without an IT team.",
    "landing.hero.lead": (
        "EGG-API publishes your library, museum or archive catalogue as a "
        "clean, standards-compliant public API — set up through a step-by-step "
        "wizard, no code required. Import from OAI-PMH, LIDO, MARC, MARCXML, "
        "EAD or CSV; expose Dublin Core back out as an OAI-PMH provider; "
        "serve IIIF manifests, search, facets, and structured records on a "
        "fixed public schema."
    ),
    "landing.cta.setup": "Start the setup wizard",
    "landing.cta.console": "Open the admin console",
    # Landing — status card
    "landing.status.heading": "Status",
    "landing.status.service": "Service",
    "landing.status.backend": "Backend",
    "landing.status.online": "online",
    "landing.status.reachable": "reachable",
    "landing.status.unreachable": "unreachable",
    "landing.status.unknown": "unknown",
    # Landing — cards
    "landing.card.public_api.heading": "Public API",
    "landing.card.oai.heading": "OAI-PMH provider",
    "landing.card.oai.body": (
        "Aggregators (Europeana, Gallica, Isidore, BASE) can harvest this deployment directly:"
    ),
    # Landing — sections
    "landing.who.heading": "Who is EGG-API for?",
    "landing.who.body": (
        "It is built for the people who actually curate heritage collections "
        "— archivists, librarians, museum registrars — and who do not have a "
        "software engineer on call. You run one binary, open the wizard, fill "
        "a few fields, and your collection is live on the web with all the "
        "right content-negotiation, rate-limiting and audit logging a GLAM "
        "institution needs."
    ),
    "landing.profiles.heading": "Three collection profiles, one public schema",
    "landing.importers.heading": "Nine importers, no custom scripts",
    "landing.importers.schedule": (
        "Every source can be scheduled (hourly / every 6 h / daily / weekly) "
        "so the catalogue refreshes itself — no cron job to maintain."
    ),
    "landing.not.heading": "What EGG-API is not",
    "landing.next.heading": "Next steps",
    # Landing — nav + footer
    "landing.nav.home": "Home",
    "landing.nav.console": "Open the console",
    "landing.nav.oai": "OAI endpoint",
    "landing.nav.openapi": "OpenAPI",
    "landing.nav.about": "About",
    "landing.footer.tagline": "heritage API for non-technical operators",
    # Landing — /about
    "about.heading": "About EGG-API",
    "about.intro": (
        "EGG-API started as a thin façade over Elasticsearch for heritage "
        "institutions. After a full product review it grew into a complete "
        "publication layer that ingests the file and protocol formats every "
        "SIGB, DAMS and archive CMS actually exports, republishes through a "
        "clean JSON API and an OAI-PMH endpoint, and lets non-technical "
        "operators drive the whole flow from a browser."
    ),
    "about.principles.heading": "Design principles",
    "about.principles.zero_it": (
        "Zero IT required. Every install decision is a form field in the "
        "admin wizard. No YAML editing, no container orchestration, no cron."
    ),
    "about.principles.boring_deps": (
        "Dependencies stay boring. FastAPI + Pydantic + httpx + stdlib XML / "
        "CSV. No pymarc, no lxml, no openpyxl, no APScheduler, no message "
        "broker — everything in a single Python process."
    ),
    "about.principles.standards": (
        "Standards out of the box. OAI-PMH in and out, IIIF manifest "
        "passthrough, Dublin Core / LIDO / MARC / EAD mappers, JSON-LD "
        "search responses. Aggregators can harvest you the day you install."
    ),
    "about.principles.no_lock_in": (
        "No scare tactics, no vendor lock-in. No Apple Developer fee, no "
        "Authenticode cert, no SaaS control plane. You own the binary, the "
        "config, the database and the index."
    ),
    "about.fits.heading": "How it fits together",
    "about.links.heading": "Links",
    # Language switcher — always show both labels in their own language
    # so users can find their own without knowing the current one.
    "lang.en": "English",
    "lang.fr": "Français",
    "lang.switch_to": "Switch language",
    # Admin shell (Sprint 30)
    "admin.title": "EGG-API Admin",
    "admin.signed_in_as": "Signed in as",
    "admin.nav.dashboard": "Dashboard",
    "admin.nav.setup": "Setup wizard",
    "admin.nav.config": "Configuration",
    "admin.nav.mapping": "Mapping",
    "admin.nav.keys": "API keys",
    "admin.nav.imports": "Data imports",
    "admin.nav.usage": "Recent activity",
    "admin.nav.help": "Help",
    "admin.nav.sign_out": "Sign out",
    "admin.nav.sign_out_everywhere": "Sign out everywhere",
    "admin.nav.lang_switch": "Switch language",
    # Imports dashboard (Sprint 30)
    "imports.page.title": "Data imports",
    "imports.page.help": (
        "Connect your library, museum or archive catalogue to EGG-API "
        "through an OAI-PMH endpoint or a flat file. The importer "
        "harvests the records into the active search backend; your "
        "public API then serves them."
    ),
    "imports.add.heading": "Add an import source",
    "imports.add.label": "Label",
    "imports.add.label_help": "A human-readable name. Shown in the dashboard and logs.",
    "imports.add.kind": "Importer kind",
    "imports.add.submit": "Add source",
    "imports.add.schedule": "Run schedule",
    "imports.add.schedule.manual": "Manual only (run via the button below)",
    "imports.add.schedule.hourly": "Every hour",
    "imports.add.schedule.6h": "Every 6 hours",
    "imports.add.schedule.daily": "Once a day",
    "imports.add.schedule.weekly": "Once a week",
    "imports.table.label": "Label",
    "imports.table.endpoint": "Endpoint",
    "imports.table.prefix": "Prefix",
    "imports.table.set": "Set",
    "imports.table.profile": "Profile",
    "imports.table.schedule": "Schedule",
    "imports.table.last_run": "Last run",
    "imports.table.next_run": "Next run",
    "imports.table.actions": "Actions",
    "imports.table.run_now": "Run now",
    "imports.table.delete": "Delete",
    "imports.table.empty": "No sources yet. Add one above.",
}

FR: dict[str, str] = {
    "landing.hero.title": "L'API patrimoniale pour les institutions sans équipe informatique.",
    "landing.hero.lead": (
        "EGG-API publie le catalogue de votre bibliothèque, musée ou service "
        "d'archives sous forme d'API publique propre et standardisée — "
        "configurée via un assistant pas à pas, sans code. Importez depuis "
        "OAI-PMH, LIDO, MARC, MARCXML, EAD ou CSV ; réexposez du Dublin Core "
        "via un fournisseur OAI-PMH ; servez des manifestes IIIF, de la "
        "recherche, des facettes et des enregistrements structurés sur un "
        "schéma public stable."
    ),
    "landing.cta.setup": "Lancer l'assistant de configuration",
    "landing.cta.console": "Ouvrir la console d'administration",
    "landing.status.heading": "État du service",
    "landing.status.service": "Service",
    "landing.status.backend": "Backend",
    "landing.status.online": "en ligne",
    "landing.status.reachable": "joignable",
    "landing.status.unreachable": "injoignable",
    "landing.status.unknown": "inconnu",
    "landing.card.public_api.heading": "API publique",
    "landing.card.oai.heading": "Fournisseur OAI-PMH",
    "landing.card.oai.body": (
        "Les agrégateurs (Europeana, Gallica, Isidore, BASE) peuvent "
        "moissonner directement cette installation :"
    ),
    "landing.who.heading": "À qui s'adresse EGG-API ?",
    "landing.who.body": (
        "Il est conçu pour les personnes qui gèrent les collections "
        "patrimoniales — archivistes, bibliothécaires, régisseurs d'œuvres — "
        "et qui n'ont pas d'informaticien sous la main. Vous lancez un "
        "binaire, ouvrez l'assistant, remplissez quelques champs, et votre "
        "collection est en ligne avec la négociation de contenu, la limitation "
        "de débit et la journalisation d'audit dont une institution GLAM a "
        "besoin."
    ),
    "landing.profiles.heading": "Trois profils de collection, un schéma public",
    "landing.importers.heading": "Neuf importeurs, aucun script à écrire",
    "landing.importers.schedule": (
        "Chaque source peut être planifiée (toutes les heures, 6 h, "
        "quotidien, hebdomadaire) pour que le catalogue se rafraîchisse "
        "tout seul — sans cron à maintenir."
    ),
    "landing.not.heading": "Ce qu'EGG-API n'est pas",
    "landing.next.heading": "Étapes suivantes",
    "landing.nav.home": "Accueil",
    "landing.nav.console": "Console d'administration",
    "landing.nav.oai": "Endpoint OAI",
    "landing.nav.openapi": "OpenAPI",
    "landing.nav.about": "À propos",
    "landing.footer.tagline": "API patrimoniale pour opérateurs non techniques",
    "about.heading": "À propos d'EGG-API",
    "about.intro": (
        "EGG-API a commencé comme une fine façade au-dessus d'Elasticsearch "
        "pour les institutions patrimoniales. Après une revue produit "
        "complète, il est devenu une couche de publication complète qui "
        "ingère les formats de fichiers et protocoles que tout SIGB, DAMS "
        "ou CMS d'archives exporte réellement, les republie via une API "
        "JSON propre et un endpoint OAI-PMH, et laisse des opérateurs non "
        "techniques piloter l'ensemble depuis un navigateur."
    ),
    "about.principles.heading": "Principes de conception",
    "about.principles.zero_it": (
        "Aucune compétence informatique requise. Chaque choix d'installation "
        "est un champ de formulaire dans l'assistant. Pas de YAML à éditer, "
        "pas d'orchestration de conteneurs, pas de cron à écrire."
    ),
    "about.principles.boring_deps": (
        "Des dépendances ennuyeuses. FastAPI + Pydantic + httpx + XML / CSV "
        "de la bibliothèque standard. Pas de pymarc, pas de lxml, pas "
        "d'openpyxl, pas d'APScheduler, pas de courtier de messages — tout "
        "dans un seul processus Python."
    ),
    "about.principles.standards": (
        "Les standards par défaut. OAI-PMH en entrée et en sortie, "
        "redirection IIIF, mappeurs Dublin Core / LIDO / MARC / EAD, "
        "réponses JSON-LD. Les agrégateurs peuvent vous moissonner dès "
        "l'installation."
    ),
    "about.principles.no_lock_in": (
        "Ni pièges, ni enfermement propriétaire. Pas de frais Apple "
        "Developer, pas de certificat Authenticode, pas de plan de contrôle "
        "SaaS. Vous possédez le binaire, la config, la base et l'index."
    ),
    "about.fits.heading": "Comment ça s'articule",
    "about.links.heading": "Liens",
    "lang.en": "English",
    "lang.fr": "Français",
    "lang.switch_to": "Changer de langue",
    # Admin shell (Sprint 30)
    "admin.title": "Console EGG-API",
    "admin.signed_in_as": "Connecté en tant que",
    "admin.nav.dashboard": "Tableau de bord",
    "admin.nav.setup": "Assistant de configuration",
    "admin.nav.config": "Configuration",
    "admin.nav.mapping": "Mapping",
    "admin.nav.keys": "Clés d'API",
    "admin.nav.imports": "Imports de données",
    "admin.nav.usage": "Activité récente",
    "admin.nav.help": "Aide",
    "admin.nav.sign_out": "Se déconnecter",
    "admin.nav.sign_out_everywhere": "Déconnecter toutes les sessions",
    "admin.nav.lang_switch": "Changer de langue",
    # Imports dashboard (Sprint 30)
    "imports.page.title": "Imports de données",
    "imports.page.help": (
        "Connectez le catalogue de votre bibliothèque, musée ou service "
        "d'archives à EGG-API via un endpoint OAI-PMH ou un fichier plat. "
        "L'importeur moissonne les enregistrements vers le moteur de "
        "recherche actif ; votre API publique les sert ensuite."
    ),
    "imports.add.heading": "Ajouter une source d'import",
    "imports.add.label": "Libellé",
    "imports.add.label_help": "Un nom lisible. Affiché dans le tableau de bord et les journaux.",
    "imports.add.kind": "Type d'importeur",
    "imports.add.submit": "Ajouter la source",
    "imports.add.schedule": "Planification",
    "imports.add.schedule.manual": "Manuel (lancer via le bouton ci-dessous)",
    "imports.add.schedule.hourly": "Toutes les heures",
    "imports.add.schedule.6h": "Toutes les 6 heures",
    "imports.add.schedule.daily": "Une fois par jour",
    "imports.add.schedule.weekly": "Une fois par semaine",
    "imports.table.label": "Libellé",
    "imports.table.endpoint": "Endpoint",
    "imports.table.prefix": "Préfixe",
    "imports.table.set": "Ensemble",
    "imports.table.profile": "Profil",
    "imports.table.schedule": "Planification",
    "imports.table.last_run": "Dernier lancement",
    "imports.table.next_run": "Prochain lancement",
    "imports.table.actions": "Actions",
    "imports.table.run_now": "Lancer maintenant",
    "imports.table.delete": "Supprimer",
    "imports.table.empty": "Aucune source pour l'instant. Ajoutez-en une ci-dessus.",
}

_CATALOGUES: dict[str, dict[str, str]] = {"en": EN, "fr": FR}


def _coerce_lang(raw: str | None) -> str | None:
    if not raw:
        return None
    head = raw.split(",", 1)[0].strip().lower()
    if not head:
        return None
    # Accept-Language values look like "fr-CA;q=0.8" — split on "-" / ";".
    head = head.split(";", 1)[0]
    head = head.split("-", 1)[0]
    if head in SUPPORTED_LANGS:
        return head
    return None


def _config_default_lang() -> str | None:
    """Return ``AppConfig.default_language`` when set, else ``None``.

    Imported lazily so :mod:`app.i18n` stays free of the container
    dependency at import time (critical: ``container`` itself pulls
    config, which pulls Pydantic models that haven't finished loading
    when i18n is imported for the first time).
    """

    try:
        from app.dependencies import container

        raw = container.config_manager.config.default_language
    except Exception:
        return None
    return _coerce_lang(raw)


def resolve_lang(request: Request | None) -> str:
    """Pick the best supported language for a request.

    ``request`` may be ``None`` (unit-test context); the resolver then
    falls through to the config → env default → English.
    """

    if request is not None:
        q = (request.query_params.get("lang") or "").strip().lower()
        coerced = _coerce_lang(q)
        if coerced:
            return coerced
        cookie = request.cookies.get(LANG_COOKIE, "")
        coerced = _coerce_lang(cookie)
        if coerced:
            return coerced
        header = request.headers.get("accept-language", "")
        coerced = _coerce_lang(header)
        if coerced:
            return coerced
    cfg_lang = _config_default_lang()
    if cfg_lang:
        return cfg_lang
    env = (os.getenv("EGG_DEFAULT_LANG", "") or "").strip().lower()
    coerced = _coerce_lang(env)
    return coerced or DEFAULT_LANG


def translator(lang: str) -> Callable[[str], str]:
    """Return a callable ``t(key)`` bound to ``lang``.

    Missing keys fall back to the English catalogue, then to the raw
    key — a typo never 500s the page.
    """

    catalogue = _CATALOGUES.get(lang, EN)
    fallback = EN

    def _t(key: str) -> str:
        if key in catalogue:
            return catalogue[key]
        if key in fallback:
            logger.debug("i18n_missing_key", extra={"lang": lang, "key": key})
            return fallback[key]
        logger.warning("i18n_unknown_key", extra={"lang": lang, "key": key})
        return key

    return _t
