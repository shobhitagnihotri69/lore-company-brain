# Lore — The Kubernetes Company Brain

> **An AI company brain built on a FalkorDB graph.** It maps how an organization *really* works (who officially owns what vs. who actually does the work, why decisions were made), answers with sources, and routes new work to the right people. Proven on public Kubernetes data. Built for **WeMakeDevs × FalkorDB Graph Hacks**.

---

## 👥 Team & Responsibilities
- **Ashish**: Data ingestion, graph modeling, query tools, and web interface.
- **A**: AI Agent (`app/agent.py` implementing `ask(question)`).
- **You (Shobhit)**: Backend server, API contracts, security, rate limiting, and deployment.
- **C**: Action router (`app/actions.py` implementing the Approve button) and demo video.

---

## ⚡ Core Value Propositions

Every organization has two org charts:
1. **The Official One (`OWNS`)**: Stated in `sigs.yaml` and leadership rosters.
2. **The Real One (`KNOWS`)**: Lives in decision history, KEP authorship, code reviews, and approvals.

### What Lore Discovered on Real Kubernetes Data
- **1,636 People, 679 Decisions (KEPs), 2,078 Merged PRs, 986 Issues, 645 Code Folders, 450 Feature Gates, 59 Releases, and 57,000+ Connections**.
- **Owns vs. Knows Gap**: In SIG Node, 3 of the top 8 contributors with the highest decision impact (`tallclair` with 31 decisions, `ffromani` with 21, `thockin` with 20) are **not** on the official team leadership chart.
- **Same gap in code review**: In each of the 5 teams we loaded, 5 or 6 of the top 8 PR reviewers are not that team's official leaders. `liggitt` (a SIG Auth lead) is a top-2 reviewer in all five.
- **Decision Trail & Replacements**: Traces decisions across an 8-year evolution (e.g. *Coscheduling* (KEP-583, 2018) &rarr; *Gang Scheduling* (KEP-4671, 2025) &rarr; *Decouple PodGroup API* (KEP-5832, 2026)).
- **Stale Decision Catch**: Surfaces decisions marked `implementable` at alpha stage that have gone untouched for 8+ releases.
- **Actionable Routing**: Triage incoming issues to the real experts and approve actions via `/api/actions/approve`.

---

## 🚀 Run It Yourself

### Option 1: Docker Compose (Recommended)

1. **Clone the repository and switch to branch `server`**:
   ```bash
   git clone https://github.com/Laimos-code/lore-company-brain.git
   cd lore-company-brain
   git checkout server
   ```

2. **Configure Environment Variables**:
   ```bash
   cp .env.example .env
   # Edit .env with your LLM keys if running live agent:
   # FALKORDB_HOST=falkordb
   # FALKORDB_PORT=6379
   # LLM_API_KEY=your_key_here
   ```

3. **Start FalkorDB and the App**:
   ```bash
   docker compose up -d
   ```
   *Note: Port 8000 is open for the web app. Ports 6379 and 3000 are kept private for database security.*

4. **Load the Kubernetes Brain Data**:
   Download the Kubernetes community and enhancement data (or use provided data sample), then run:
   ```bash
   # From host (connecting to localhost:6379 forwarded internally):
   FALKORDB_HOST=127.0.0.1 FALKORDB_PORT=6379 python scripts/load_k8s_brain.py data/
   ```
   To also load PRs, issues and code owners, follow [📥 Load the data](#-load-the-data).

5. **Open Lore in your browser**:
   Navigate to [http://localhost:8000](http://localhost:8000) and click any of the example questions!

---

### Option 2: Run Locally on Your Laptop (Without Docker Compose)

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Start FalkorDB with Docker**:
   ```bash
   docker run -p 127.0.0.1:6379:6379 -d --name falkordb falkordb/falkordb:latest
   ```

3. **Fetch Kubernetes Data**:
   ```bash
   mkdir -p data/community data/enhancements
   # Download Kubernetes SIGs registry:
   curl -sSL https://raw.githubusercontent.com/kubernetes/community/master/sigs.yaml -o data/community/sigs.yaml
   # Clone or sparse-checkout enhancements KEPs:
   git clone --depth 1 --filter=blob:none --sparse https://github.com/kubernetes/enhancements.git data/enhancements_repo
   cd data/enhancements_repo && git sparse-checkout set keps && cd ../..
   mv data/enhancements_repo/keps data/enhancements/keps
   ```

4. **Load Data into FalkorDB**:
   ```bash
   FALKORDB_HOST=localhost FALKORDB_PORT=6379 python scripts/load_k8s_brain.py data/
   ```
   To also load PRs, issues and code owners, follow [📥 Load the data](#-load-the-data).

5. **Start the FastAPI Server**:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

6. **Open the Web UI**:
   Visit [http://localhost:8000](http://localhost:8000).

---

## 📥 Load the data

Run these from the repo root while FalkorDB is up. Downloads stay in `data/`, which is not committed.

1. **Teams and design decisions (KEPs).** Download `sigs.yaml` and the KEPs (Option 2, step 3), then:
   ```bash
   FALKORDB_HOST=localhost python scripts/load_k8s_brain.py data/
   ```
   This step starts the `k8s` graph from scratch. Steps 2 and 3 only add to it, so they are safe to re-run.

2. **GitHub PRs and issues** for sig-node, sig-scheduling, sig-auth, sig-storage and sig-network (12 months).
   Put `GITHUB_TOKEN=...` in `.env`. Every answer is cached in `data/raw/`, so a re-run continues where it stopped.
   ```bash
   python scripts/fetch_github.py
   FALKORDB_HOST=localhost python scripts/load_github.py
   ```

3. **Official code owners** from the `OWNERS` files of `kubernetes/kubernetes` (downloads only those files, about 6 MB):
   ```bash
   git clone --depth 1 --filter=blob:none --sparse https://github.com/kubernetes/kubernetes data/k8s-owners
   git -C data/k8s-owners sparse-checkout set --no-cone "**/OWNERS" "/OWNERS_ALIASES"
   FALKORDB_HOST=localhost python scripts/load_owners.py
   ```
   On Windows Git Bash, start the `sparse-checkout` line with `MSYS_NO_PATHCONV=1`. Otherwise Git Bash turns
   `/OWNERS_ALIASES` into a Windows path and that file is never downloaded.

## 🧠 Graph model (FalkorDB graph `k8s`)

Counts from the full load on 2026-10-18: **6,852 nodes and 57,379 edges**.
GitHub data: `kubernetes/kubernetes` PRs merged and issues created from 2025-10-18 to 2026-10-18 with a
`sig/node`, `sig/scheduling`, `sig/auth`, `sig/storage` or `sig/network` label.

| Node | Key properties | Count | Loaded by |
| :--- | :--- | ---: | :--- |
| `Person` | `login` (lowercase), `display` | 1,636 | every loader |
| `Team` | `key` (e.g. `sig-node`), `name`, `kind` | 52 | `load_k8s_brain.py` (`sigs.yaml`) |
| `Subproject` | `name` | 240 | `load_k8s_brain.py` |
| `KEP` | `num`, `title`, `status`, `stage`, `created`, `latest`, `path` (its folder in kubernetes/enhancements) | 679 | `load_k8s_brain.py` (`kep.yaml`) |
| `FeatureGate` | `name` | 450 | `load_k8s_brain.py` |
| `Component` | `name` | 27 | `load_k8s_brain.py` |
| `Release` | `version` | 59 | `load_k8s_brain.py` |
| `PR` | `num`, `title`, `created`, `merged` | 2,078 | `load_github.py` |
| `Issue` | `num`, `title`, `created`, `closed`, `state` | 986 | `load_github.py` |
| `Folder` | `path` (e.g. `pkg/scheduler/framework`) | 645 | `load_github.py` |

```
DECISIONS (load_k8s_brain.py)                            count
(Person)-[:LEADS {role}]->(Team)                           174
(Team)-[:HAS_SUBPROJECT]->(Subproject)                     241
(Person)-[:AUTHORED]->(KEP)                              1,149
(Person)-[:REVIEWED]->(KEP)                              1,952
(Person)-[:APPROVED]->(KEP)                              1,286
(KEP)-[:OWNED_BY]->(Team)                                  683
(KEP)-[:INVOLVES]->(Team)                                  487
(KEP)-[:REPLACES]->(KEP)                                     9
(KEP)-[:SEE_ALSO]->(KEP)                                   298
(KEP)-[:INTRODUCES]->(FeatureGate)                         478
(FeatureGate)-[:USED_IN]->(Component)                      741
(KEP)-[:REACHED {stage}]->(Release)                      1,273

WORK (load_github.py)
(Person)-[:AUTHORED {at}]->(PR)                          2,078
(Person)-[:REVIEWED {at, state}]->(PR)                   3,328
(Person)-[:LGTM {at}]->(PR)                              2,136   a comment had a /lgtm line
(Person)-[:APPROVED {at}]->(PR)                          1,600   a comment had an /approve line
(Person)-[:AUTHORED {at}]->(Issue)                         986
(Person)-[:COMMENTED {at}]->(Issue)                      2,484
(Person)-[:ASSIGNED]->(Issue)                              614
(PR)-[:FOR_TEAM]->(Team)                                 3,131
(Issue)-[:FOR_TEAM]->(Team)                              1,046
(PR)-[:TOUCHES]->(Folder)                               10,172

OFFICIAL CODE OWNERS (load_owners.py, from OWNERS files)
(Person)-[:APPROVER_OF {source}]->(Folder)               9,285
(Person)-[:REVIEWER_OF {source}]->(Folder)              11,748
```

How to read it:
- **One Person per login.** Logins are lowercased, fixed with `data/aliases.yaml` and bots are dropped (one shared rule in `scripts/logins.py`).
- **`AUTHORED`, `REVIEWED` and `APPROVED` point to both KEPs and PRs**, so always label the target in queries: `(k:KEP)` or `(pr:PR)`.
- **`at`** is the person's earliest date on that PR or issue (ISO text such as `2026-01-05T12:34:56Z`, which sorts correctly as text).
- **`REVIEWED`** is one edge per person per PR. `state` is their strongest verdict: `APPROVED`, then `CHANGES_REQUESTED`, then `COMMENTED`.
- **The author doesn't count as their own reviewer.** A PR author's own reviews, `/lgtm` and `/approve` aren't counted, and neither are comments on their own issue.
- **`FOR_TEAM`** links an item to every slice team whose `sig/*` label it has (519 PRs have two or more).
- **`Folder`** is the first 3 folder levels of a changed file. Under `staging/src/` it's the 3 levels after that (e.g. `staging/src/k8s.io/kubelet/pkg`), so each staging library gets its own folders.
- **`APPROVER_OF` / `REVIEWER_OF`** follow Prow's rules: the nearest OWNERS file plus its parents, stopping at `no_parent_owners`. `source` is the OWNERS file that lists the person, e.g. `pkg/scheduler/OWNERS`.

Top PR reviewers of a team, marked if they are official leaders (`scripts/load_github.py` prints this for all 5 teams):
```cypher
MATCH (:Team {key:$team})<-[:FOR_TEAM]-(:PR)<-[e:REVIEWED|LGTM|APPROVED]-(p:Person)
WITH p, count(e) AS total ORDER BY total DESC LIMIT 8
OPTIONAL MATCH (p)-[:LEADS]->(lt:Team)
RETURN p.display, total, $team IN collect(lt.key) AS official_leader ORDER BY total DESC
```

## ⚠️ Data quirks

- OWNERS files are today's version, not historical, so the reviewer test compares against today's official list.
- `/lgtm cancel` and `/approve cancel` count as LGTM and APPROVED: the download keeps only a yes/no flag per comment, not its text.
- The download keeps the first 100 changed files and 50 reviews of a PR; 74 PRs have more files and 74 more reviews.
- `ASSIGNED` has no date: the download doesn't include when someone was assigned.
- Files in the repo root (`go.mod`, `go.sum`, ...) have no `Folder`.
- `liggit` (11 KEP links) is a typo for `liggitt` and is merged via `data/aliases.yaml`. More likely KEP typos
  (e.g. `ligitt`, `saadali`, `gnuified`) are not merged yet: each needs a person to confirm it first.
- In `OWNERS` files, every list under `filters:` is merged. So `staging/OWNERS`'s approvers for `go.mod`/`go.sum` count as approvers of every staging folder.

---

## 🌐 Putting It Online (Cloud Server Deployment)

To host Lore on a small cloud server (DigitalOcean, AWS EC2, Hetzner, or GitHub Student Pack credits):

### 1. Requirements & Security Checklist
- **Single Public HTTPS Link**: e.g., `https://lore.yourdomain.com` or via Cloudflare Tunnel.
- **Port Security**: Only the application port (80/443) is exposed to the internet. FalkorDB ports (6379, 3000) are bound strictly to `127.0.0.1` or the internal Docker bridge network.
- **Data Persistence**: Uses a Docker volume (`falkordb_data`) so all graph nodes survive container reboots.

### 2. Deployment Steps
1. SSH into the cloud server:
   ```bash
   ssh ubuntu@your-server-ip
   ```
2. Clone and start:
   ```bash
   git clone https://github.com/Laimos-code/lore-company-brain.git
   cd lore-company-brain
   docker compose up -d
   ```
3. Load the data once:
   ```bash
   FALKORDB_HOST=127.0.0.1 FALKORDB_PORT=6379 python3 scripts/load_k8s_brain.py data/
   ```
4. Set up HTTPS with Caddy (automatic Let's Encrypt certificates):
   ```caddy
   lore.yourdomain.com {
       reverse_proxy localhost:8000
   }
   ```
   Or use Cloudflare Tunnel without opening any inbound ports.

---

## 📡 API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Serves the interactive web interface (`web/index.html`). |
| `GET` | `/api/health` | Returns service health & FalkorDB connection status (`{"ok": true, "falkordb": true}`). |
| `POST` | `/api/ask` | Queries Lore brain with 60-second timeout guard and 20 req/min rate limit per IP. |
| `POST` | `/api/actions/approve` | Approves an issue routing triage, draft comment, or decision ping. |
| `GET` | `/api/actions/history` | Lists approved action history. |

See [`docs/tool_contract.md`](docs/tool_contract.md) for full JSON request and response payloads.

---

## 🧪 Testing

Run automated tests:
```bash
pytest -o pythonpath=. tests/test_server.py
```
All 6 tests verify page serving, FalkorDB health ping, verified stub response, decision trails, rate limiting enforcement, and approve action flows.

The graph tools (`app/tools.py`: `who_owns`, `who_knows`, `why`, `whats_stale`, `route_issue`; contract in
[`docs/tool_contract.md`](docs/tool_contract.md) section 4) are tested on the real graph. These tests skip
themselves until all three loaders in [📥 Load the data](#-load-the-data) have run:
```bash
pytest -o pythonpath=. tests/test_tools.py
```
