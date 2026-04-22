# Spécification technique complète — EGG API

## 1. Objet du document

Ce document définit la spécification précise d’un logiciel nommé **EGG API**, dont le but est de publier rapidement une **API patrimoniale standardisée** au-dessus d’un backend documentaire ou de recherche existant, sans réécriture du système source.

Le document est écrit pour pouvoir servir directement de base de génération de code par un LLM ou de travail pour une équipe de développement.

Il décrit :

- le périmètre du produit ;
- les comportements attendus ;
- les modules à développer ;
- les endpoints publics et d’administration ;
- les schémas de données ;
- les règles métier ;
- les contraintes de sécurité ;
- les adaptateurs backend ;
- les critères d’acceptation.

---

## 2. But du produit

Le produit doit permettre à une institution GLAM (bibliothèque, musée, archives, médiathèque, portail patrimonial, réseau documentaire) de :

1. détecter un backend existant ;
2. configurer simplement quels contenus rendre publics ;
3. exposer une API REST JSON stable et documentée ;
4. appliquer des politiques de sécurité et de débit ;
5. gérer des clés d’API ;
6. observer l’usage de l’API ;
7. éventuellement exposer des liens IIIF ;
8. pouvoir ensuite servir de base à un connecteur MCP.

Le produit **ne remplace pas** le moteur source, le SIGB, le DAMS ou le portail documentaire. Il agit comme une **façade normalisatrice et protectrice**.

---

## 3. Terminologie

### 3.1 Record

Un **record** est l’unité publique exposée par l’API. Il peut correspondre à une notice bibliographique, une notice d’objet de musée, une notice d’archive, une image, un dossier, un périodique, une œuvre, un manuscrit, etc.

### 3.2 Backend

Le **backend** est le système réellement interrogé : Elasticsearch, OpenSearch, Solr dans la V1.

### 3.3 Adapter

Un **adapter** est un module logiciel qui traduit les requêtes de la EGG API en requêtes natives du backend.

### 3.4 Public API

La **Public API** est l’interface de consultation exposée aux clients.

### 3.5 Admin API

L’**Admin API** est l’interface d’administration du logiciel.

### 3.6 Security profile

Un **security profile** est un profil prédéfini qui configure le niveau d’ouverture, les limites, l’authentification et la surface d’exposition.

---

## 4. Objectifs fonctionnels

Le système doit impérativement permettre :

1. la recherche plein texte simple ;
2. la recherche filtrée ;
3. la pagination ;
4. le tri borné ;
5. la récupération d’une notice par identifiant ;
6. la récupération de facettes sur champs autorisés ;
7. l’exposition de métadonnées normalisées ;
8. l’exposition de liens externes ou IIIF si présents ;
9. la documentation automatique de l’API ;
10. la création et la gestion de clés d’API ;
11. la limitation de débit ;
12. l’observabilité minimale ;
13. une installation simple par assistant.

---

## 5. Objectifs non fonctionnels

Le système doit :

1. être déployable en lecture seule ;
2. ne jamais exécuter de requête libre transmise telle quelle au backend ;
3. protéger le backend des usages intensifs non contrôlés ;
4. produire une réponse JSON stable quel que soit le backend ;
5. être exploitable sans compétence avancée en Elastic, Solr ou OpenSearch ;
6. conserver un temps de réponse acceptable sur usages ordinaires ;
7. être extensible par adaptateurs.

---

## 6. Périmètre V1

> **Note sur la v1.0.0 livrée** — la V1 décrite ici a été livrée à
> l'exception de l'adapter **Solr** et de l'**installateur graphique
> séquentiel** (§26), tous deux reportés. L'état courant est consigné
> dans `CHANGELOG.md` et `app/TODO.md`. Solr reste au périmètre cible,
> les endpoints ES/OpenSearch sont déjà en production.

### 6.1 Inclus

- Support Elasticsearch
- Support OpenSearch
- Support Solr *(reporté post-v1.0.0 ; tracké dans `app/TODO.md`)*
- API publique REST JSON
- Documentation OpenAPI auto-générée
- Console d’administration simplifiée
- Clés d’API
- Quotas simples
- Profils de sécurité
- Cache HTTP pour GET
- Journaux de requêtes
- Détection du backend
- Mapping champ interne → champ public
- Liens IIIF si déjà présents dans les données

### 6.2 Exclus V1

- Indexation native de nouvelles données
- Édition des données source
- Réécriture du schéma documentaire interne
- Exposition directe d’un DSL backend
- Requêtes analytiques libres
- Exports bulk illimités
- Authentification fédérée complexe
- Interface de transformation avancée des métadonnées
- Serveur MCP natif

---

## 7. Architecture logique

Le système comporte les modules suivants :

### 7.1 Installer

Assistant d’installation et de configuration initiale.

### 7.2 Detection Engine

Moteur qui teste des signatures de backend et valide la connectivité.

### 7.3 Adapter Layer

Couche de traduction entre la EGG API et les backends.

### 7.4 Schema Mapper

Composant qui convertit les champs backend vers le schéma public `record`.

### 7.5 Query Policy Engine

Composant qui valide les paramètres, applique les profils de sécurité et normalise les requêtes.

### 7.6 Public API Service

Service HTTP public exposant les endpoints de consultation.

### 7.7 Admin API Service

Service HTTP réservé à l’administration.

### 7.8 API Gateway Integration

Couche de gestion des clés, quotas, trafic, journalisation et cache.

### 7.9 Metrics & Logs

Composant qui collecte métriques, erreurs, usages et journaux.

---

## 8. Contrat général de l’API publique

### 8.1 Style général

- API REST
- JSON UTF-8
- horodatages en ISO 8601
- pagination par `page` et `page_size` dans la V1
- réponses enveloppées de manière stable

### 8.2 Versionnement

L’API doit être versionnée par chemin :

- `/v1/...`

### 8.3 En-têtes obligatoires de réponse

- `Content-Type: application/json; charset=utf-8`
- `X-Request-Id`
- `X-RateLimit-Limit` si auth ou quotas actifs
- `X-RateLimit-Remaining` si quotas actifs
- `X-RateLimit-Reset` si quotas actifs

### 8.4 En-têtes optionnels

- `ETag`
- `Cache-Control`
- `Last-Modified`

### 8.5 Authentification publique

Trois modes possibles :

- public anonyme ;
- clé d’API optionnelle ;
- clé d’API obligatoire.

Clé transmise par défaut dans :

- `X-API-Key`

---

## 9. Schéma public minimal

### 9.1 Schéma `Record`

```json
{
  "id": "string",
  "type": "string",
  "title": "string|null",
  "subtitle": "string|null",
  "description": "string|null",
  "creators": [
    {
      "name": "string",
      "role": "string|null",
      "identifier": "string|null"
    }
  ],
  "contributors": [
    {
      "name": "string",
      "role": "string|null",
      "identifier": "string|null"
    }
  ],
  "date": {
    "display": "string|null",
    "sort": "string|null",
    "start": "string|null",
    "end": "string|null"
  },
  "languages": ["string"],
  "subjects": ["string"],
  "keywords": ["string"],
  "collection": {
    "id": "string|null",
    "label": "string|null"
  },
  "holding_institution": {
    "id": "string|null",
    "label": "string|null"
  },
  "identifiers": {
    "source_id": "string|null",
    "ark": "string|null",
    "doi": "string|null",
    "isbn": "string|null",
    "issn": "string|null",
    "call_number": "string|null"
  },
  "links": {
    "source": "string|null",
    "thumbnail": "string|null",
    "preview": "string|null",
    "iiif_manifest": "string|null",
    "iiif_image": "string|null",
    "rights": "string|null"
  },
  "media": [
    {
      "type": "image|audio|video|text|pdf|other",
      "url": "string",
      "label": "string|null"
    }
  ],
  "rights": {
    "label": "string|null",
    "uri": "string|null",
    "license": "string|null"
  },
  "availability": {
    "public": true,
    "digital": true,
    "iiif": false
  },
  "raw_identifiers": ["string"],
  "raw_fields": {},
  "backend": {
    "engine": "elasticsearch|opensearch|solr|other",
    "index_or_collection": "string|null"
  },
  "timestamps": {
    "indexed_at": "string|null",
    "updated_at": "string|null"
  }
}
```

### 9.2 Règles de schéma

- `id` est obligatoire.
- `type` est obligatoire.
- `title` peut être `null` si la donnée source ne l’a pas.
- `raw_fields` n’est exposé que si le profil de sécurité l’autorise.
- `backend` n’est pas exposé aux clients publics par défaut ; il peut être activé en mode expert.

---

## 10. Schémas de réponse API publique

### 10.1 `SearchResponse`

```json
{
  "meta": {
    "request_id": "string",
    "page": 1,
    "page_size": 20,
    "returned": 20,
    "total": 1234,
    "has_next": true,
    "sort": "relevance",
    "query_time_ms": 32
  },
  "query": {
    "q": "manuscrit enluminé",
    "filters": {
      "type": ["manuscript"],
      "language": ["fre"]
    }
  },
  "facets": [
    {
      "name": "type",
      "label": "Type",
      "buckets": [
        { "value": "manuscript", "count": 87 },
        { "value": "book", "count": 30 }
      ]
    }
  ],
  "items": [
    {
      "id": "rec_001",
      "type": "manuscript",
      "title": "Heures à l’usage de Paris",
      "description": "string",
      "date": {
        "display": "XVe siècle",
        "sort": "1450"
      },
      "links": {
        "thumbnail": "https://...",
        "source": "https://...",
        "iiif_manifest": "https://..."
      }
    }
  ],
  "links": {
    "self": "/v1/search?q=manuscrit&page=1&page_size=20",
    "next": "/v1/search?q=manuscrit&page=2&page_size=20",
    "prev": null
  }
}
```

### 10.2 `RecordResponse`

```json
{
  "meta": {
    "request_id": "string"
  },
  "item": {}
}
```

### 10.3 `FacetResponse`

```json
{
  "meta": {
    "request_id": "string",
    "query_time_ms": 12
  },
  "facets": [
    {
      "name": "language",
      "label": "Langue",
      "buckets": [
        { "value": "fre", "count": 200 },
        { "value": "lat", "count": 54 }
      ]
    }
  ]
}
```

### 10.4 `ErrorResponse`

```json
{
  "error": {
    "code": "invalid_parameter",
    "message": "Parameter page_size exceeds allowed maximum",
    "details": {
      "parameter": "page_size",
      "max_allowed": 100
    },
    "request_id": "string"
  }
}
```

---

## 11. Endpoints publics obligatoires

### 11.1 `GET /v1/search`

#### But

Recherche dans les records publics exposés.

#### Paramètres autorisés

- `q`: string, optionnel, recherche plein texte simple
- `page`: integer >= 1, défaut 1
- `page_size`: integer >= 1, borné par profil
- `sort`: string, valeurs autorisées par configuration
- `facet`: string répétable, demande explicite de facettes
- `include_fields`: string CSV optionnel, liste blanche uniquement
- `type`: string répétable
- `collection`: string répétable
- `language`: string répétable
- `institution`: string répétable
- `date_from`: string ou integer selon mapping
- `date_to`: string ou integer selon mapping
- `subject`: string répétable
- `has_digital`: boolean
- `has_iiif`: boolean
- `cursor`: non supporté en V1, réservé futur

#### Règles métier

- Si `q` est absent, l’endpoint peut soit retourner tous les résultats paginés, soit refuser selon configuration.
- `page_size` doit être borné par profil.
- Les filtres doivent être appliqués uniquement sur les champs whitelistés.
- `include_fields` ne peut pas demander un champ non exposé.
- `sort` ne peut prendre que des valeurs autorisées.
- `facet` ne peut porter que sur des facettes autorisées.
- Aucun paramètre ne doit être injecté tel quel dans le backend.
- Toute requête doit passer par le Query Policy Engine.

#### Valeurs de tri recommandées

- `relevance`
- `date_asc`
- `date_desc`
- `title_asc`
- `title_desc`
- `updated_desc`

#### Comportement backend

- Le service construit une représentation intermédiaire `NormalizedQuery`.
- L’adapter traduit `NormalizedQuery` vers la syntaxe backend.
- Le résultat brut backend est mappé vers `SearchResponse`.

#### Cas de refus

- `page_size` trop grand → `400`
- tri non autorisé → `400`
- facette interdite → `400`
- clé invalide → `401`
- quota dépassé → `429`

### 11.2 `GET /v1/records/{id}`

#### But

Retourner une notice publique complète par identifiant.

#### Paramètres

- `id` dans le chemin, obligatoire
- `include_raw`: boolean, autorisé seulement en mode expert/admin

#### Règles métier

- L’identifiant doit être résolu vers la source réelle.
- Si l’objet n’existe pas → `404`.
- Si l’objet existe mais n’est pas public → `404` par défaut, ou `403` si profil expert.
- La réponse doit contenir le `Record` normalisé.
- Les liens IIIF doivent être inclus s’ils existent et sont autorisés.

### 11.3 `GET /v1/facets`

#### But

Retourner des distributions de facettes sur un périmètre de recherche.

#### Paramètres

Même base que `/v1/search` avec :

- `facet`: obligatoire, répétable
- `page`: non autorisé
- `page_size`: non autorisé

#### Règles métier

- Si aucun `facet` n’est fourni → `400`.
- Seules les facettes whitelistées sont autorisées.
- Le nombre de buckets par facette est borné par profil et configuration.
- Les facettes doivent être calculées sur le jeu filtré.

### 11.4 `GET /v1/health`

#### But

Retourner un état synthétique du service.

#### Réponse

```json
{
  "status": "ok|degraded|error",
  "service": "egg-api",
  "version": "1.0.0",
  "backend": {
    "engine": "elasticsearch",
    "status": "ok",
    "latency_ms": 12
  },
  "timestamp": "2026-03-11T10:00:00Z"
}
```

#### Règles métier

- Cet endpoint ne doit pas exposer de secrets.
- L’état backend doit être synthétique.
- Un mode public restreint peut retourner seulement `{status, version}`.

### 11.5 `GET /v1/openapi.json`

#### But

Retourner la spécification OpenAPI générée dynamiquement ou statiquement.

#### Règles métier

- La spec doit refléter les champs, filtres et facettes activés.
- Les endpoints non activés ne doivent pas y figurer.
- Les exemples doivent correspondre à la configuration réelle.

---

## 12. Endpoints publics optionnels V1

### 12.1 `GET /v1/collections`

Retourne les collections publiques exposées.

### 12.2 `GET /v1/suggest`

Retourne des suggestions simples d’autocomplétion.

#### Paramètres

- `q`: obligatoire
- `field`: optionnel, liste blanche
- `size`: borné

### 12.3 `GET /v1/manifest/{id}`

Retourne ou redirige vers un manifest IIIF si configuré.

#### Modes possibles

- `proxy` : l’API renvoie le manifest récupéré en amont
- `redirect` : l’API redirige vers l’URL manifest source
- `link-only` : l’API renvoie un JSON avec le lien

### 12.4 `GET /v1/schema`

Expose le schéma public actif de la ressource `Record`.

---

## 13. Admin API

Tous les endpoints d’administration doivent être sous `/admin/v1/...`.

Authentification obligatoire.

### 13.1 `POST /admin/v1/setup/detect`

#### But

Détecter le backend.

#### Entrée

```json
{
  "url": "https://backend.example.org",
  "auth": {
    "type": "none|basic|api_key|bearer",
    "username": "string|null",
    "password": "string|null",
    "api_key": "string|null",
    "token": "string|null"
  }
}
```

#### Sortie

```json
{
  "detected": true,
  "engine": "elasticsearch",
  "version": "8.x",
  "collections": ["catalogue_public"],
  "capabilities": {
    "search": true,
    "facets": true,
    "highlight": false,
    "suggest": true
  }
}
```

### 13.2 `POST /admin/v1/setup/scan-fields`

#### But

Scanner un index ou une collection pour proposer un mapping.

#### Entrée

```json
{
  "source": "catalogue_public",
  "sample_size": 100
}
```

#### Sortie

Liste des champs observés, types estimés, taux de remplissage.

### 13.3 `POST /admin/v1/setup/create-config`

#### But

Créer une configuration de projet.

#### Entrée

Doit inclure :

- backend choisi
- source choisie
- security profile
- mapping des champs
- facettes autorisées
- tris autorisés
- auth publique

#### Sortie

Configuration persistée + validation.

### 13.4 `GET /admin/v1/config`

Retourne la configuration active.

### 13.5 `PUT /admin/v1/config`

Met à jour la configuration active.

### 13.6 `POST /admin/v1/config/validate`

Valide la configuration sans l’appliquer.

### 13.7 `POST /admin/v1/keys`

Crée une clé d’API.

### 13.8 `GET /admin/v1/keys`

Liste les clés existantes.

### 13.9 `DELETE /admin/v1/keys/{key_id}`

Révoque une clé.

### 13.10 `PATCH /admin/v1/keys/{key_id}`

Met à jour quotas, label, statut.

### 13.11 `GET /admin/v1/usage`

Retourne statistiques d’usage.

### 13.12 `GET /admin/v1/logs`

Retourne un échantillon de logs filtrables.

### 13.13 `POST /admin/v1/test-query`

Exécute une requête test via le Query Policy Engine et renvoie la traduction backend ainsi que le résultat normalisé.

### 13.14 `GET /admin/v1/status`

Retourne statut complet du service.

### 13.15 `POST /admin/v1/export-config`

Exporte la config en YAML ou JSON.

### 13.16 `POST /admin/v1/import-config`

Importe une config existante.

---

## 14. Politique de dérive de schéma et erreurs de mapping

Le système doit traiter explicitement la disparition, le renommage ou l’incompatibilité de champs backend après une évolution d’index, de collection ou de schéma.

### 14.1 États possibles d’un champ mappé

Chaque champ configuré doit être classé à l’exécution dans l’un des états suivants :

- `ok` : le champ existe et son type est compatible ;
- `missing` : le champ n’existe plus dans la source ;
- `incompatible_type` : le champ existe mais son type n’est plus compatible avec le mapping ;
- `empty_source` : le champ existe mais ne contient plus de valeur exploitable dans l’échantillon courant ;
- `degraded` : le champ peut être partiellement interprété mais avec perte d’information.

### 14.2 Politique obligatoire par niveau de criticité

Chaque entrée de mapping doit pouvoir déclarer une criticité :

- `required` : si le champ est `missing` ou `incompatible_type`, la configuration est considérée invalide pour les endpoints qui en dépendent ;
- `recommended` : le champ peut être omis de la réponse, mais un avertissement doit être produit ;
- `optional` : le champ peut disparaître silencieusement de la charge utile, mais un événement doit être journalisé.

### 14.3 Comportement d’exécution

- Le système ne doit jamais inventer une valeur de remplacement non documentée.
- Si un champ `required` servant à `id` ou `type` devient indisponible, le service doit passer en état `degraded` ou `error` selon le nombre d’endpoints affectés.
- Si un champ `required` non structurel disparaît, les endpoints dépendants doivent renvoyer une erreur de type `configuration_error` jusqu’à correction ou remapping.
- Si un champ `recommended` disparaît, le record reste servi sans ce champ, avec émission d’un avertissement admin visible dans `/admin/v1/status` et `/admin/v1/logs`.
- Si un champ `optional` disparaît, le record reste servi sans ce champ et un log de niveau `warning` doit être produit au plus une fois par fenêtre de temps configurable pour éviter le bruit.
- Les fallbacks silencieux ne sont autorisés que pour les champs explicitement marqués `optional`.

### 14.4 Endpoints d’observabilité associés

L’Admin API doit exposer :

- un résumé des mappings dégradés dans `/admin/v1/status` ;
- la liste détaillée des champs affectés dans un sous-objet `mapping_health` ;
- un endpoint optionnel `/admin/v1/mapping/health` pour inspection fine.

### 14.5 Validation préventive

Le système doit exécuter périodiquement ou au démarrage un contrôle de compatibilité du mapping avec la source.

Une modification détectée doit produire :

- un statut `ok`, `warning` ou `error` ;
- une liste des champs impactés ;
- une recommandation d’action.

---

## 15. Modèle de configuration

Le produit doit persister une configuration déclarative.

### 15.1 Exemple YAML

```yaml
project:
  name: egg-public-api
  api_version: v1

backend:
  engine: elasticsearch
  url: https://search.example.org
  auth:
    type: api_key
    api_key_env: BACKEND_API_KEY
  source: catalogue_public

security:
  profile: prudent
  public_access: true
  require_api_key: false
  rate_limit_per_minute: 60
  page_size_default: 20
  page_size_max: 50

exposure:
  allow_empty_query: false
  allow_raw_fields: false
  include_backend_metadata: false

mapping:
  id:
    path: id
    criticality: required
  type:
    path: doc_type
    criticality: required
  title:
    path: title
    criticality: recommended
  description:
    path: abstract
    criticality: optional
  creators:
    - path: creator
      mode: split_list
      criticality: optional
  date_display:
    path: date_display
    criticality: recommended
  date_sort:
    path: date_sort
    criticality: recommended
  subjects:
    path: subjects
    criticality: optional
  language:
    path: lang
    criticality: optional
  collection_id:
    path: collection_id
    criticality: optional
  collection_label:
    path: collection_label
    criticality: optional
  source_url:
    path: permalink
    criticality: recommended
  thumbnail_url:
    path: thumbnail
    criticality: optional
  iiif_manifest_url:
    path: manifest_url
    criticality: optional

filters:
  allowed:
    - type
    - collection
    - language
    - date_from
    - date_to
    - subject

facets:
  allowed:
    - type
    - collection
    - language

sorts:
  allowed:
    - relevance
    - date_desc
    - title_asc
```

---

## 16. Représentation intermédiaire obligatoire

Le système doit créer une structure intermédiaire indépendante du backend.

### 16.1 `NormalizedQuery`

```json
{
  "text": "manuscrit enluminé",
  "page": 1,
  "page_size": 20,
  "sort": "relevance",
  "filters": {
    "type": ["manuscript"],
    "language": ["fre"],
    "date_from": "1400",
    "date_to": "1500"
  },
  "facets": ["type", "language"],
  "include_fields": ["id", "title", "date", "links.thumbnail"]
}
```

Aucun adapter n’a le droit de recevoir directement les paramètres HTTP bruts.

---

## 17. Interface des adaptateurs backend

Chaque adapter doit implémenter l’interface suivante.

### 17.1 Méthodes obligatoires

- `detect(connection_config) -> DetectionResult`  
  Détecte si le backend correspond à l’adapter.

- `health(connection_config) -> BackendHealth`  
  Retourne l’état synthétique du backend.

- `list_sources(connection_config) -> list[SourceInfo]`  
  Retourne les index/collections sélectionnables.

- `scan_fields(connection_config, source, sample_size) -> list[FieldProfile]`  
  Retourne les champs détectés et leurs types probables.

- `validate_mapping(connection_config, source, mapping_config) -> MappingValidationResult`  
  Valide que les champs configurés existent et sont exploitables.

- `search(connection_config, source, normalized_query, runtime_config) -> RawSearchResult`  
  Exécute la recherche.

- `get_record(connection_config, source, id, runtime_config) -> RawRecordResult`  
  Retourne un enregistrement brut.

- `get_facets(connection_config, source, normalized_query, runtime_config) -> RawFacetResult`  
  Retourne les facettes.

- `suggest(connection_config, source, q, field, size, runtime_config) -> RawSuggestResult`  
  Optionnel, sinon `NotImplemented`.

- `translate_query(normalized_query, runtime_config) -> dict`  
  Retourne la forme native de la requête pour debug/admin.

### 17.2 Contraintes

- Un adapter ne doit jamais désactiver les bornes de sécurité du Query Policy Engine.
- Un adapter doit fonctionner uniquement en lecture.
- Un adapter doit renvoyer des erreurs typées et normalisées.

---

## 18. Règles de mapping de champs

### 18.1 Modes de mapping supportés

- `direct`
- `constant`
- `split_list`
- `join_list`
- `first_non_empty`
- `template`
- `nested_object`
- `date_parser`
- `boolean_cast`
- `url_passthrough`

### 18.2 Exemple

Si le backend a :

- `creator_name`
- `creator_role`

Le mapper doit pouvoir produire :

```json
{
  "creators": [
    {
      "name": "Jean Dupont",
      "role": "author"
    }
  ]
}
```

### 18.3 Dates

Le mapper doit pouvoir gérer :

- chaîne d’affichage ;
- valeur de tri ;
- bornes `start` / `end`.

---

## 19. Query Policy Engine

Le Query Policy Engine est obligatoire. Il constitue le cœur de sécurité fonctionnelle.

### 19.1 Fonctions obligatoires

- `parse_http_request(request) -> RawClientQuery`  
  Lit les paramètres HTTP.

- `normalize(raw_query, runtime_config) -> NormalizedQuery`  
  Transforme les paramètres vers la forme intermédiaire.

- `validate(normalized_query, runtime_config) -> ValidationResult`  
  Vérifie la conformité.

- `apply_profile_limits(normalized_query, runtime_config) -> NormalizedQuery`  
  Applique limites de taille, facettes, tris, etc.

- `compute_cache_key(normalized_query, runtime_config) -> string`  
  Calcule une clé de cache stable.

- `redact_for_logs(normalized_query) -> dict`  
  Produit une représentation journalisable sans secret.

### 19.2 Règles obligatoires

- limiter `page_size`
- limiter le nombre de filtres
- limiter le nombre de facettes
- interdire les champs inconnus
- interdire les opérateurs non supportés
- interdire les requêtes backend natives
- normaliser booléens et dates
- produire des messages d’erreur explicites

---

## 20. Profils de sécurité

### 20.1 Profil prudent

- `allow_empty_query = false`
- `public_access = true`
- `require_api_key = false` configurable
- `page_size_default = 20`
- `page_size_max = 50`
- `max_facets = 3`
- `max_buckets_per_facet = 20`
- `allow_raw_fields = false`
- `allow_debug_translation = false`
- `cache_enabled = true`
- `rate_limit_per_minute = 60`
- tris très limités

### 20.2 Profil standard

- `allow_empty_query = true` configurable
- `page_size_max = 100`
- `max_facets = 5`
- `max_buckets_per_facet = 50`
- quotas plus ouverts

### 20.3 Profil diffusion

- conçu pour partenaires ou gros usages
- nécessite clé d’API
- quotas plus élevés
- cache renforcé
- observabilité plus détaillée

### 20.4 Profil expert

- autorise certains champs bruts
- debug avancé
- plus de personnalisation
- jamais de requête libre native

---

## 21. Gestion des clés d’API

### 21.1 Modèle `ApiKey`

```json
{
  "id": "key_001",
  "label": "Portail de recherche universitaire",
  "status": "active|revoked|suspended",
  "created_at": "string",
  "expires_at": "string|null",
  "quota": {
    "requests_per_minute": 120,
    "requests_per_day": 20000
  },
  "permissions": {
    "public_search": true,
    "record_read": true,
    "facets": true,
    "suggest": false,
    "admin": false
  }
}
```

### 21.2 Règles

- la valeur secrète de la clé ne doit être affichée qu’une seule fois à la création ;
- seule l’empreinte doit être stockée ;
- les clés doivent pouvoir être révoquées immédiatement ;
- le système doit associer l’usage à une clé quand elle existe.

---

## 22. Gestion du cache

### 22.1 Cache HTTP

- GET uniquement
- basé sur la requête normalisée
- invalide si la config change

### 22.2 Cache applicatif

Optionnel, mais recommandé pour :

- `/search`
- `/facets`
- `/collections`
- `/openapi.json`

### 22.3 Paramètres recommandés

- TTL court pour `/search`
- TTL moyen pour `/facets`
- TTL long pour `/openapi.json`

---

## 23. Journalisation et observabilité

### 23.1 À journaliser

- `request_id`
- `endpoint`
- temps de réponse
- code HTTP
- clé d’API ou anonymisé
- backend ciblé
- taille de réponse
- erreur éventuelle

### 23.2 À ne jamais journaliser

- valeur secrète des clés
- credentials backend
- payloads sensibles non nécessaires

### 23.3 Métriques minimales

- nombre de requêtes par endpoint
- taux d’erreur
- p50 / p95 de latence
- top filtres utilisés
- top facettes utilisées
- top clés par volume

---

## 24. Codes d’erreur normalisés

Le système doit toujours répondre avec un code métier stable.

### 24.1 Liste minimale

- `invalid_parameter`
- `missing_parameter`
- `invalid_api_key`
- `quota_exceeded`
- `forbidden`
- `not_found`
- `backend_unavailable`
- `configuration_error`
- `unsupported_operation`
- `internal_error`

### 24.2 Correspondance HTTP

- `400` → `invalid_parameter`, `missing_parameter`
- `401` → `invalid_api_key`
- `403` → `forbidden`
- `404` → `not_found`
- `409` → `configuration_error`
- `429` → `quota_exceeded`
- `502/503` → `backend_unavailable`
- `500` → `internal_error`

---

## 25. Détection automatique des backends

### 25.1 Elasticsearch

Détection par :

- endpoint racine ou cluster health
- signature version / tagline / structure réponse

### 25.2 OpenSearch

Détection par :

- endpoint cluster health
- structure de version OpenSearch

### 25.3 Solr *(reporté post-v1.0.0)*

Détection par :

- endpoint système Solr
- API collections
- API v2

> Statut v1.0.0 : aucun adapter Solr n'est livré. Les règles de détection
> ci-dessus sont conservées comme cahier des charges pour l'ajout futur.

### 25.4 Règles

- si plusieurs détections réussissent, proposer un classement de confiance ;
- si aucune détection ne réussit, permettre un mode manuel ;
- ne jamais persister les secrets backend en clair dans les journaux.

---

## 26. Interface utilisateur minimale de l’installateur

Le logiciel doit avoir au minimum les écrans suivants :

### 26.1 Écran 1 — Connexion au backend

Champs :

- URL
- type d’auth
- identifiants
- bouton Détecter

### 26.2 Écran 2 — Choix de la source

- liste index/collections
- test rapide de volume

### 26.3 Écran 3 — Mapping

- proposition automatique des champs
- validation manuelle

### 26.4 Écran 4 — Profil de sécurité

- prudent / standard / diffusion / expert
- texte explicatif non technique

### 26.5 Écran 5 — Exposition

- champs publics
- filtres autorisés
- facettes autorisées
- tris autorisés
- activer IIIF si détecté

### 26.6 Écran 6 — Clés et accès

- public anonyme / clé recommandée / clé obligatoire

### 26.7 Écran 7 — Validation et test

- exemple de requête
- exemple de réponse
- erreurs éventuelles

---

## 27. Pagination et stratégie de profondeur

### 27.1 Pagination V1

La V1 utilise une pagination par `page` et `page_size` pour simplicité d’usage côté institutions et intégrateurs.

### 27.2 Limites obligatoires

- Le système doit convertir `page` / `page_size` en stratégie backend sûre.
- Le système ne doit pas autoriser une profondeur de pagination non bornée.
- Une limite de profondeur configurable doit être définie, par exemple `max_result_window` ou équivalent logique.
- Si la requête dépasse la profondeur autorisée, l’API doit renvoyer une erreur `invalid_parameter` ou `unsupported_operation` avec message explicite indiquant que la pagination profonde n’est pas supportée en V1.
- L’API ne doit pas tenter de contourner silencieusement les limites backend au prix d’une dégradation sévère de performance.

### 27.3 Stratégie backend recommandée

- Pour les premières pages, l’adapter peut utiliser l’équivalent de `from/size` ou du mécanisme natif approprié.
- Au-delà d’un seuil configurable, le service doit refuser la requête en V1 plutôt que simuler une pagination profonde coûteuse.
- La documentation OpenAPI et la documentation utilisateur doivent indiquer clairement que la pagination profonde n’est pas garantie.

### 27.4 Évolution future

Une V2 pourra introduire une pagination par curseur ou jeton opaque pour les usages intensifs. Cette capacité est explicitement hors périmètre V1.

---

## 28. Spécification détaillée du comportement de recherche

### 28.1 Recherche texte

- si `q` est fourni, la recherche doit l’utiliser selon le champ ou groupe de champs configuré ;
- les champs de recherche par défaut doivent être configurables ;
- la pondération peut être simple mais configurable.

### 28.2 Filtres

- les filtres répétés se combinent par OR sur un même champ ;
- des champs différents se combinent par AND ;

Exemple : `type=book&type=manuscript&language=fre` = `(type=book OR type=manuscript) AND language=fre`

### 28.3 Dates

- `date_from` et `date_to` filtrent sur le champ de tri ou la borne normalisée ;
- si le backend ne peut pas filtrer exactement, une approximation documentée peut être utilisée.

### 28.4 Tri

- `relevance` n’est autorisé que si `q` est présent ou si le backend a une stratégie cohérente ;
- `date_desc`, `date_asc`, `title_asc`, `title_desc` doivent être configurables.

### 28.5 Facettes

- calculées sur le jeu filtré courant ;
- jamais plus de `max_buckets_per_facet` ;
- ordre par count décroissant sauf config contraire.

### 28.6 Include fields

- si `include_fields` est fourni, le service doit réduire le payload mais conserver `id` et `type` ;
- les champs internes non publics restent inaccessibles.

---

## 29. Règles sur les liens IIIF

Le système ne génère pas du IIIF à partir de rien en V1.

Il doit seulement :

- détecter des champs IIIF existants ;
- exposer `iiif_manifest` ;
- exposer `iiif_image` si disponible ;
- éventuellement proxifier ou rediriger les manifests.

Si aucune donnée IIIF n’existe, le système ne doit pas fabriquer une URL supposée.

---

## 30. Sécurité opérationnelle

### 30.1 Obligatoire

- lecture seule côté backend ;
- séparation secrets backend / secrets publics ;
- stockage sécurisé des secrets ;
- désactivation des debug sensibles en production ;
- CORS configurable ;
- timeouts backend ;
- retry limité ;
- circuit breaker simple recommandé.

### 30.2 Interdit

- stocker la clé backend en clair dans les logs ;
- exposer la requête backend complète au public ;
- exposer des endpoints de configuration sans auth ;
- autoriser une recherche native transmise telle quelle.

---

## 31. CORS

Le système doit offrir :

- `off`
- `same-origin`
- `allowlist`
- `wide-open` uniquement après confirmation explicite admin

Configuration recommandée : `allowlist`.

---

## 32. Multi-tenant / multi-collection

V1 doit supporter uniquement :

- un projet = une source principale ;
- une configuration active = une instance API publique.

Le multi-tenant est explicitement hors périmètre V1.  
Le multi-projet dans une seule instance de service est également hors périmètre V1.

Une source unique peut contenir plusieurs collections logiques exposées comme filtres ou facettes, mais cela ne constitue pas un support multi-tenant. Aucune promesse de mutualisation de plusieurs institutions autonomes dans une même instance ne doit être faite en V1.

---

## 33. Interface d’administration

### 33.1 Nature de l’interface

La V1 doit fournir :

- une interface web d’administration comme mode principal ;
- une Admin API complète utilisable par intégrateurs et automatisation.

La V1 n’a pas l’obligation de fournir un CLI complet.  
Un CLI minimal peut exister pour le bootstrap, l’import/export de configuration ou les environnements sans interface graphique, mais il est optionnel.

### 33.2 Principe de conception

Comme la cible primaire comprend des professionnels GLAM peu techniques, toute opération essentielle doit être réalisable depuis l’interface web d’administration sans édition manuelle de fichier.

### 33.3 Fonctions minimales de l’interface web

- connexion au backend ;
- détection ;
- scan des champs ;
- mapping ;
- choix du profil de sécurité ;
- activation des champs, filtres, facettes, tris ;
- test de requêtes ;
- création/révocation de clés ;
- consultation de l’état du service et des avertissements de mapping.

---

## 34. OpenAPI à générer

La spécification OpenAPI doit inclure :

- infos service
- serveurs
- schémas `Record`, `SearchResponse`, `ErrorResponse`
- paramètres communs réutilisables
- exemples réalistes
- sécurité si clé d’API active
- description des limites par endpoint

---

## 35. Critères d’acceptation fonctionnels

### 35.1 Installation

- un administrateur peut connecter un backend supporté ;
- le système détecte correctement Elastic / OpenSearch (et, une fois livré, Solr) dans un cas standard ;
- une configuration valide peut être créée sans édition manuelle de fichier.

### 35.2 Recherche

- `/v1/search` retourne des résultats normalisés ;
- pagination fonctionnelle ;
- filtres appliqués correctement ;
- tri borné respecté.

### 35.3 Notice

- `/v1/records/{id}` retourne un record normalisé complet.

### 35.4 Sécurité

- un paramètre interdit renvoie `400` ;
- une clé invalide renvoie `401` ;
- un dépassement de quota renvoie `429` ;
- aucune requête native libre n’est possible.

### 35.5 Administration

- création et révocation de clé possibles ;
- export config possible ;
- statistiques minimales disponibles.

---

## 36. Tests à écrire obligatoirement

### 36.1 Tests unitaires

- parsing des paramètres
- validation des filtres
- limitation de `page_size`
- mapping des champs
- adaptation backend
- codes d’erreur

### 36.2 Tests d’intégration

- backend Elastic simulé
- backend OpenSearch simulé
- backend Solr simulé *(à livrer avec l'adapter Solr)*
- recherche réelle sur petit dataset
- facettes réelles
- lecture record

### 36.3 Tests de sécurité

- injection de paramètres non autorisés
- dépassement de quotas
- accès admin sans auth
- debug interdit en mode prudent

### 36.4 Tests de charge minimaux

- charge ordinaire sur `/search`
- facettes sur gros index
- résilience backend lent

---

## 37. Arborescence logicielle recommandée

```text
/app
  /public_api
  /admin_api
  /adapters
    /elasticsearch
    /opensearch
    /solr
  /query_policy
  /mappers
  /schemas
  /config
  /auth
  /rate_limit
  /cache
  /metrics
  /logging
  /installer
  /tests
```

---

## 38. Technologies recommandées pour l’implémentation

Ce document n’impose pas un langage, mais pour une génération LLM efficace :

- backend API : Python + FastAPI ou TypeScript + NestJS/Express ;
- validation : Pydantic ou Zod ;
- config : YAML + variables d’environnement ;
- cache : Redis optionnel ;
- gateway : intégration simple ou middleware natif ;
- tests : pytest ou vitest/jest.

---

## 39. Contraintes LLM pour génération de code

Si le code est généré par un LLM, le LLM doit impérativement respecter :

- séparation claire des modules ;
- typage strict ;
- aucune logique backend dans les routes ;
- toute validation dans le Query Policy Engine ;
- adaptateurs backend interchangeables ;
- tests générés avec fixtures ;
- erreurs normalisées ;
- documentation inline minimale ;
- pas d’exposition de secret dans les logs ;
- lecture seule strictement garantie.

---

## 40. MVP de développement

Pour produire un premier logiciel utilisable, l’ordre d’implémentation doit être :

### Phase 1

- schémas publics
- Query Policy Engine
- adapter Elasticsearch
- `/v1/search`
- `/v1/records/{id}`
- `/v1/health`
- `/v1/openapi.json`

### Phase 2

- facettes
- clés d’API
- quotas
- admin setup minimal
- scan de champs

### Phase 3

- adapter OpenSearch
- adapter Solr
- export/import config
- suggest
- IIIF link resolver

---

## 41. Ce que l’API doit faire, résumé impératif

L’API doit :

- rendre interrogeables des données patrimoniales déjà indexées ;
- normaliser les réponses ;
- protéger le backend ;
- permettre une installation simple ;
- exposer un contrat public stable ;
- rester indépendante du moteur sous-jacent ;
- être exploitable par des humains, des scripts et plus tard des agents.

L’API ne doit pas :

- exposer le moteur source brut ;
- exécuter des requêtes libres ;
- promettre une compatibilité automatique absolue ;
- masquer les limites de sécurité ;
- fabriquer de faux liens IIIF ;
- se substituer au système documentaire source.

---

## 42. Suite recommandée

Après implémentation du cœur V1, les évolutions les plus logiques sont :

- module OAI-PMH optionnel ;
- module SRU optionnel ;
- génération automatique d’un connecteur MCP ;
- multi-projets ;
- analytics plus avancées ;
- tableau d’administration plus complet.

---

## 43. Fin de spécification

Ce document constitue la base de référence pour le développement. Toute ambiguïté doit être résolue en faveur de :

- la sécurité du backend ;
- la stabilité du contrat public ;
- la simplicité d’installation ;
- l’indépendance vis-à-vis du moteur sous-jacent.
