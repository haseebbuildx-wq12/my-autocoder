# SCOPE OF THIS RUN: PHASE 1 ONLY

This specification is being built by an autonomous coding agent on a Linux CI runner.
Implement ONLY Phase 1 (section "PHASE 1" at the end). Do NOT implement Phases 2-10; their files exist only
as the documented stubs that Phase 1 requires (rule C3 "Stub rule"). References in Part A to later phases are context only.

## Environment for this run (overrides any Windows/local wording)

- Runs on Linux (GitHub Actions). Node.js 20, npm, PostgreSQL 16 service on localhost:5432. Docker is NOT used.
- `DATABASE_URL` is provided as an environment variable (database `whatsapp_saas` already exists and is empty).
  Never create or commit a `.env` file; create `.env.example` only. Read configuration from process.env.
- Test-only values for SESSION_SECRET, APP_ENCRYPTION_KEY, CRON_SECRET, APP_ENV, APP_URL, API_URL, PORT, CORS_ORIGINS
  are provided in the environment. Do not hard-code any secret.
- All scripts must be cross-platform Node/TypeScript (no bash-only commands). `pg_dump`/`psql` path comes from the
  optional env var `PG_BIN_PATH` (default: use the binary on PATH).
- Verification commands must be a single program with arguments (no pipes, no `&&`, no `python -c`/`node -e`).
  Use `npm run check`, `npm test`, `npm run doctor`, `npm run db:migrate`.
- The generated project lives in the project root given to you; the control files go in `project-control/`.

# SaaS WhatsApp AI Automation Platform
## Master Development Specification v2 (10 Phases)

**Stack:** Next.js (React) · Node.js + TypeScript · REST API · Services → Repositories · PostgreSQL
**Model:** Multi-tenant SaaS, 10 phases, AI-assisted development (Codex / Claude Code)
**Status:** Final architecture baseline. Do not redesign without explicit instruction.

---

# PART A — HOW THIS PROJECT IS RUN (READ FIRST)

## A1. Golden workflow

1. **Phase 1 creates the ENTIRE project skeleton in one go**: every folder, every planned file (as a documented stub), all control files, config, DB connection, migration runner, installer.
2. **Phase 2 creates the ENTIRE UI**: every page, layout and component with empty/loading/error states and typed API client (mock data allowed). Later phases only *fill* pages with real API calls, never re-design them.
3. From Phase 3 onward each phase **fills stubs** with real logic. Files are not moved or renamed unless FILE_MAP.md is updated.
4. After **every task** the AI must update the control files (A2) and tick the TODO.
5. After **every phase** the AI must regenerate the SQL dumps and verify a clean install from an empty DB.

## A2. Control files (live in `/project-control/`, attached to every AI session)

| File | Purpose | Updated when |
|---|---|---|
| `MASTER_SPEC.md` | This document (source of truth) | Only by the owner |
| `TODO.md` | Smallest requirements, short and simple, with checkboxes | After every task |
| `DB_SCHEMA.md` | Every table and every column, with phase added | After every migration |
| `FILE_MAP.md` | Every file, its job, its phase, status (stub / partial / done) | After every file created/changed |
| `API_ROUTES.md` | Every REST route, permission, request/response shape | After every route |
| `PROGRESS_LOG.md` | What changed per session (short) | End of each session |
| `DEBUG_LOG.md` | Bugs found, cause, fix, how verified | On every bug |

**Rule:** Before any task the AI reads `TODO.md`, `FILE_MAP.md`, `DB_SCHEMA.md`. If DB is needed, it reads `DB_SCHEMA.md` first and does not guess columns.

### TODO.md format

```
# TODO
Legend: [ ] todo  [~] in progress  [x] done  [!] blocked/bug

## PHASE 1 — Foundation
- [x] Create monorepo folders
- [ ] .env loader with validation
- [ ] DB pool (pg)
- [ ] Migration runner (up/down)
...
## BUGS
- [!] P3: login returns 500 when tenant missing (see DEBUG_LOG #4)
## LATE-ADDED REQUIREMENTS
- [ ] (anything owner adds later goes here, tiny + simple)
```
Rules: one line = one small checkable thing. No paragraphs. New requirement mid-project → add under LATE-ADDED, then move to right phase.

### DB_SCHEMA.md format

```
## table: users            (added: P3)  tenant-scoped: no (global)
| column | type | null | default | key/index | note |
| id | uuid | no | gen_random_uuid() | PK | |
| email | citext | no | | UNIQUE | login |
| password_hash | text | no | | | argon2id |
| created_at | timestamptz | no | now() | | |
### changes
- P5: added `last_login_at timestamptz null`
```
Every tenant table shows `business_id uuid NOT NULL FK → businesses(id)` and its index.

### FILE_MAP.md format

```
| path | phase | status | responsibility | depends on |
| apps/api/src/modules/orders/order.service.ts | P8 | stub | order lifecycle, totals, confirmation | order.repository, delivery.service |
```

### Header comment required in EVERY source file

```ts
/**
 * FILE: apps/api/src/modules/orders/order.service.ts
 * PHASE: 8   STATUS: stub | partial | done
 * PURPOSE: Order lifecycle logic. No SQL here (use repository).
 * DEPENDS: order.repository, delivery.service, event bus
 */
```

### SQL dump rules (installation must always be easy)

- `/database/dumps/empty_schema.sql` — full schema + required seed (roles, permissions, default plans, AI defaults), **no business data**. Regenerated after every phase.
- `/database/dumps/full_with_demo.sql` — optional demo tenant data.
- Scripts: `npm run db:dump` (via pg_dump), `npm run db:import`, `npm run db:reset`, `npm run db:migrate`, `npm run db:seed`.
- Dump must import into a fresh empty PostgreSQL DB with **zero manual steps**.
- Migrations are the source of truth; dump is generated from them and must be verified by importing it into a temp DB.

### Debug & check rules (every phase)

- After finishing tasks run: `npm run check` = typecheck + lint + tests + build + route smoke test + fresh-DB import test.
- Log every bug in `DEBUG_LOG.md` (symptom, root cause, fix, test that proves it).
- Add health endpoint `/api/health` and self-diagnostic `npm run doctor` (env, DB, migrations, write permissions).
- If a requirement is missing or ambiguous, the AI adds a `[ ]` line in TODO under LATE-ADDED and continues; it does not silently invent behaviour.

---

# PART B — VISION & CORE PRINCIPLES

One platform → many businesses → isolated tenants. Each business has: WhatsApp, AI agent, products, customers, orders, knowledge, rules, team.

Same app supports restaurants, garments, e-commerce, electronics, real estate, salons, retail, services and future industries **without code changes**.

**Principles (non-negotiable):**
1. Business behaviour is **data/config**, never code. No `if (industry === 'restaurant')`. No hard-coded prompts, prices, policies.
2. **Tenant isolation:** every tenant table has `business_id`; enforced in backend repositories (and PostgreSQL Row-Level Security as a second layer). Frontend filtering is never security.
3. **AI never touches the database.** AI → controlled Tool → Service → Repository → DB.
4. **Provider abstraction** for WhatsApp and AI (interfaces + adapters).
5. **Server-side RBAC** and **server-side tool permissions.**
6. **Secrets never reach the frontend** (provider tokens, AI keys, DB password, encryption key). Provider credentials encrypted at rest (AES-256-GCM).
7. **Thin controllers, logic in services, SQL in repositories, external APIs in providers.**
8. Dynamic system prompt assembled at runtime, never stored as one final blob.
9. Delivery estimates and prices always come from backend, never AI-invented.
10. AI Router is deterministic and cheap; never call the expensive model to decide about calling it.

---

# PART C — ARCHITECTURE

## C1. Layers

```
Next.js (web)  →  REST  →  Controller (thin, validation, auth)
                              ↓
                          Service (business logic, events)
                              ↓
                        Repository (SQL, tenant filter)
                              ↓
                          PostgreSQL

External:  Service → Provider Interface → Adapter → External API
AI:        Message → Analyzer → Router → Strategy → Model/Tool → Policy Engine → Service → Response
```

## C2. Tech choices (fixed)

- **API:** Node.js 20+, TypeScript strict, **Fastify or Express** (pick one in Phase 1, record in PROGRESS_LOG), **Zod** validation, **pg** with SQL migrations (node-pg-migrate or plain SQL runner) — no heavy ORM, so tenant filter stays explicit in repositories.
- **Auth:** HTTP-only secure cookie sessions (stored in DB) or JWT access+refresh; argon2id hashing; CSRF token for cookie flows; rate limiting.
- **Jobs (no Redis required):** PostgreSQL-backed queue table using `FOR UPDATE SKIP LOCKED`; a worker process (`npm run worker`) plus optional cron-hit endpoint protected by `CRON_SECRET`. Retries with backoff.
- **Realtime inbox:** Server-Sent Events (fallback: polling). No mandatory websocket server.
- **Web:** Next.js App Router, TypeScript, Tailwind CSS + component primitives, TanStack Query, React Hook Form + Zod. Dark navy/charcoal theme, red→purple gradient accent.
- **Tests:** Vitest/Jest (unit + integration with test DB), Playwright (few E2E).
- **Deployment:** `.env`, `npm run setup`, optional Docker Compose, PM2/systemd sample; also a `/install` web wizard.

## C3. Monorepo structure — created COMPLETELY in Phase 1

```
saas-wa-ai/
├── project-control/
│   ├── MASTER_SPEC.md  TODO.md  DB_SCHEMA.md  FILE_MAP.md
│   ├── API_ROUTES.md   PROGRESS_LOG.md  DEBUG_LOG.md
├── apps/
│   ├── api/
│   │   ├── src/
│   │   │   ├── server.ts  app.ts
│   │   │   ├── config/        env.ts  paths.ts  constants.ts
│   │   │   ├── core/          db.ts  logger.ts  errors.ts  container.ts
│   │   │   │                  tenantContext.ts  eventBus.ts  jobQueue.ts
│   │   │   │                  crypto.ts  validator.ts  pagination.ts  response.ts
│   │   │   ├── middleware/    auth  csrf  tenant  permission  rateLimit
│   │   │   │                  errorHandler  requestId  webhookVerify
│   │   │   ├── modules/       (each: *.routes.ts *.controller.ts *.service.ts
│   │   │   │                   *.repository.ts *.schema.ts *.types.ts)
│   │   │   │   auth  users  businesses  roles  permissions  plans  subscriptions
│   │   │   │   settings  audit  system-logs  installer  health
│   │   │   │   whatsapp-accounts  webhooks  messages  conversations  customers
│   │   │   │   knowledge  products  categories  attributes  delivery
│   │   │   │   cart  orders  team  notifications  usage  billing  analytics
│   │   │   │   ai-agents  ai-tools-config  ai-admin  ai-usage  cost-tracking
│   │   │   ├── providers/
│   │   │   │   ├── whatsapp/  WhatsAppProvider.interface.ts  meta.adapter.ts
│   │   │   │   │              thirdparty.adapter.ts  providerFactory.ts
│   │   │   │   └── ai/        AIProvider.interface.ts  openai.adapter.ts
│   │   │   │                  anthropic.adapter.ts  gemini.adapter.ts  providerFactory.ts
│   │   │   ├── ai/
│   │   │   │   ├── orchestrator.ts  messageAnalyzer.ts  router.ts
│   │   │   │   ├── deterministic/   rules.ts  responses.ts
│   │   │   │   ├── promptBuilder.ts  policyEngine.ts  costCalculator.ts
│   │   │   │   ├── knowledgeRetriever.ts  confidence.ts
│   │   │   │   └── tools/  registry.ts  searchProducts.ts  getProductDetails.ts
│   │   │   │               checkStock.ts  getBusinessInfo.ts  searchKnowledge.ts
│   │   │   │               getFaq.ts  getDeliveryEstimate.ts  getCustomer.ts
│   │   │   │               createCustomer.ts  createOrder.ts  getOrderStatus.ts
│   │   │   │               updateOrder.ts  requestHumanAgent.ts
│   │   │   ├── events/     eventTypes.ts  listeners/ (notification, analytics,
│   │   │   │               customerHistory, usage, crmPlaceholder)
│   │   │   ├── jobs/       sendMessage.job.ts  retryWebhook.job.ts
│   │   │   │               usageAggregate.job.ts  subscriptionCheck.job.ts
│   │   │   ├── installer/  requirements.ts  installService.ts  lock.ts
│   │   │   └── extensions/ courier  payments  crm  campaigns  voice  vision
│   │   │                   (interfaces only until Phase 10)
│   │   ├── tests/  unit/  integration/  fixtures/
│   │   └── package.json  tsconfig.json
│   └── web/
│       ├── src/app/
│       │   ├── (public)/  login  forgot-password  install
│       │   ├── (super-admin)/admin/  dashboard businesses plans subscriptions
│       │   │     whatsapp-providers ai-providers ai-models ai-routing ai-usage
│       │   │     revenue costs logs audit system settings
│       │   └── (business)/app/  dashboard inbox conversations customers
│       │         products categories attributes knowledge delivery orders
│       │         whatsapp ai-agent ai-rules ai-tools ai-routing ai-test
│       │         analytics team notifications usage billing settings audit
│       ├── src/components/  ui/ layout/ tables/ forms/ feedback/ inbox/ charts/
│       ├── src/lib/  apiClient.ts  auth.ts  permissions.ts  format.ts
│       ├── src/hooks/  src/styles/  src/types/
│       └── package.json  next.config.js
├── database/  migrations/  seeds/  dumps/ (empty_schema.sql, full_with_demo.sql)
├── scripts/   setup.ts  doctor.ts  db-dump.ts  db-import.ts  check.ts
├── storage/   logs/  uploads/  temp/
├── .env.example  docker-compose.yml  README.md  DEPLOYMENT_GUIDE.md  LICENSE
```

**Stub rule (Phase 1):** every listed file exists with the header comment, exported empty interface/class/function throwing `NotImplemented`, and a row in `FILE_MAP.md`. Nothing is "to be created later" except new files that FILE_MAP does not list.

## C4. Environment variables (`.env.example`)

```
APP_NAME=  APP_ENV=  APP_URL=  API_URL=  PORT=  APP_DEBUG=
DATABASE_URL=postgres://user:pass@host:5432/dbname
DB_SSL=false  DB_POOL_MAX=10
SESSION_SECRET=  APP_ENCRYPTION_KEY=  CRON_SECRET=  CORS_ORIGINS=
AI_PROVIDER_KEYS_JSON=  (or individual OPENAI_API_KEY etc., server only)
WHATSAPP_WEBHOOK_VERIFY_TOKEN=
INSTALLED=false
```
Only variables prefixed `NEXT_PUBLIC_` may be used in the frontend, and none may be a secret. No absolute paths; use `paths.ts` helpers (`rootPath(), storagePath(), publicPath()`).

## C5. Multi-tenancy design

- `TenantContext` built from authenticated session server-side (AsyncLocalStorage). `business_id` from frontend is **ignored**.
- Every repository method for a tenant table **requires** `businessId` (or reads it from TenantContext) and always adds `WHERE business_id = $n`.
- PostgreSQL RLS policies on tenant tables using `SET LOCAL app.business_id` per transaction (defence in depth).
- Super Admin operates cross-tenant only through dedicated admin services; every such action is audit-logged.
- Integration tests: user of Business A requests Business B resources → must be 404/403 for every module.

## C6. RBAC

Roles: Owner, Admin, Manager, Agent, Viewer (plus platform `super_admin`). Tables: `roles`, `permissions`, `role_permissions`, per-business overrides optional. Permission keys like `orders.read`, `orders.update`, `conversations.reply`, `settings.manage`, `ai.configure`. Middleware `requirePermission('orders.update')`. Frontend hides UI using the same keys but the server decides.

## C7. AI architecture

```
Message → MessageAnalyzer (regex/keywords/length/intents, no LLM)
        → Router → DETERMINISTIC | CHEAP | ADVANCED
Cheap → (low confidence / unable_to_answer / complexity_detected) → Advanced
Model → Tool selection → PolicyEngine (business tool permissions, limits)
      → Backend Tool → Service → Repository → DB → Response
```
- **Level 0:** small limited list (greetings, thanks, bye) configurable per business/language; zero API cost.
- **Level 1:** cheap model with tools (hours, location, delivery to city, payment, availability).
- **Level 2:** advanced model (multi-constraint shopping, dates, budgets, multiple tool calls).
- Provider/model configuration lives in DB (`ai_providers`, `ai_models`, `ai_routing_rules`), Super Admin defaults + business overrides.
- **Runtime prompt =** core rules + business info + agent config + business rules + retrieved knowledge (RAG-ready; Postgres full-text search first, pgvector later) + customer context + conversation context + allowed tools.
- Every request logged in `ai_usage` (provider, model, level, tokens in/out, est. cost, latency, tool calls, success/fail, fallback flag); tools in `ai_tool_logs`.

## C8. WhatsApp architecture

`WhatsAppProvider` interface: `sendText`, `sendMedia`, `sendTemplate`, `verifyWebhook`, `parseWebhook`, `getStatus`, `testConnection`, `markRead`. Adapters: Meta Cloud API, third-party, future. Webhooks: verify signature → store raw in `webhook_events` (unique provider message id → duplicate protection) → enqueue job → 200 quickly. Outgoing via job queue with retry/backoff.

## C9. Events

`eventBus.emit(event)` → listeners. Events: MessageReceived, MessageSent, OrderCreated, OrderConfirmed, OrderDelivered, WhatsAppConnected, WhatsAppDisconnected, AIEscalated, UsageWarning, UsageLimitReached, SubscriptionExpired. Persist to `events` / `event_logs`. Modules never call each other's internals for side effects; they emit events.

## C10. Security checklist

Auth, RBAC, tenant isolation, CSRF, rate limiting, webhook signature verification, encrypted credentials, audit logs, input validation (Zod on every route), secure uploads (type/size check, outside web root), duplicate webhook protection, retries, error logging without leaking secrets, security headers (helmet), backups (`db:dump`), secret masking in logs.

---

---

# PART D — PHASE 1

## PHASE 1 — Project Skeleton, Config, DB, Installer Core

**Objective:** Whole project structure exists, runs, connects to PostgreSQL, has migration system, installer, control files, dumps. Nothing business-y yet.

**Do:**
- Create the complete folder/file tree from C3 as documented stubs; create all 7 control files; fill `FILE_MAP.md` with every stub.
- Fill for real: `env.ts` (validated), `paths.ts`, `db.ts` (pg pool, transaction helper), `logger.ts` (file + console, redaction), `errors.ts` (AppError types), `response.ts`, `validator.ts`, `errorHandler`, `requestId`, `container.ts`, `eventBus.ts` (basic), `crypto.ts` (AES-GCM encrypt/decrypt, hashing), `jobQueue.ts` (table + enqueue/claim/retry).
- Migration runner + first migrations: `system_logs`, `settings`, `jobs`, `events`, `installer_state`, `schema_migrations`.
- Health module: `GET /api/health`.
- Installer API (`/api/install/*`): requirements check, DB test, run migrations+seed, save config, create Super Admin (uses `users` — add minimal `users` table here or in P3; record decision in DB_SCHEMA), finalize + **lock** (`installer_state.locked = true`; also `INSTALLED=true`). Locked installer returns 404 forever.
- Scripts: `setup`, `doctor`, `db:migrate`, `db:seed`, `db:reset`, `db:dump`, `db:import`, `check`.
- `docker-compose.yml` (postgres + api + web) as optional; README + DEPLOYMENT_GUIDE first draft.
- `.env.example`, ESLint/Prettier/tsconfig strict, CI script.

**DB tables:** `schema_migrations, system_logs, settings, jobs, events, event_logs, installer_state` (+ `users` minimal if installer needs it).

**Done when:** `npm run setup` on empty DB works; `/api/health` OK; `npm run doctor` green; importing `empty_schema.sql` into a blank DB works; installer locks; all stub files listed in FILE_MAP; TODO Phase 1 all `[x]`.

**END OF PHASE 1 SPECIFICATION**
