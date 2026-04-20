# AGENTS.md

## Project
EGG-API is a plug-and-play API layer for GLAM collections.

## Current repository state
This repository is at an initial stage.
Preserve the existing LICENSE and .gitignore.
Rewrite README.md into a full project README.
Build the first working MVP from scratch.

## Product goal
Expose a safe, normalized, backend-agnostic public API on top of an existing GLAM search backend.

## MVP scope
- Elasticsearch adapter only
- FastAPI app
- Public API
- Admin API
- YAML config
- API keys
- in-memory rate limiting
- tests

## Core rules
- Read-only against backend
- Never expose raw backend DSL publicly
- Always pass public queries through QueryPolicyEngine
- Prefer explicit errors over silent fallback
- Resolve ambiguity in favor of backend safety and contract stability
- No multi-tenant in v1
- No deep pagination workaround in v1

## Coding style
- Python 3.10+
- FastAPI
- Pydantic v2
- pytest
- strict typing
- small modules
- thin routes
- business logic in services

## Definition of done
- runnable application
- passing tests
- modular codebase
- complete README
- example config
- explicit deferred TODOs only for out-of-scope features
