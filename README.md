# NestFeast
## 1. Running code
### 1.1. Create venv
```
python -m venv .venv
```

### 1.2 Activate venv
#### 1.2.1. For powershell
```
.venv/Scripts/Activate.ps1
```

#### 1.2.2. For Mac/Linux
```
source .venv/bin/activate
```

### 1.3. Download dependencies
```
pip install -r requirements.txt
```

### 1.4. Run BE (Create new terminal for BE)
```
cd backend
uvicorn app.main:app --reload --port 8000 --log-level info
```

### 1.5. Run FE (Create new terminal for FE)
```
cd frontend
python -m streamlit run app.py  
```

### 1.6. Run Pinggy (Create new terminal for Pinggy)
```
python pinggy_run.py
```

### 1.7. Get the Pinggy link
From the output of pinggy_run.py, copy the link

## 2. Directory structure
```text
nestfeast/
├─ backend/
│  ├─ app/
│  │  ├─ api/
│  │  ├─ providers/
│  │  ├─ schemas/
│  │  ├─ services/
│  │  └─ main.py
│  ├─ .env
│  
└─ frontend/
|  ├─ .image/
|  ├─ .streamlit/
|  ├─ app.py
|  ├─ backend_client.py
|─ pinggy_run.py
|_ requirements.txt
```
### 2.1. Backend (FastAPI)

#### `backend/`
Directory containing the entire backend codebase (API + business logic).

---

#### `backend/app/main.py`
FastAPI entry point:

- Initialize `FastAPI()`.
- Mount routers from `app.api`.
- Configure docs, health checks, etc.

---

#### `backend/app/api/` – HTTP layer (routers)
Contains endpoints only (no complex business logic):

- `intake.py` – API receiving origin (text/coordinates) → calls the `intake_origin` service.
- `nearbySearch.py` – API to find POIs near the origin by radius and tag.
- `ranked.py` – API recommending ranked places (combining nearby + matrix + ranking).
- `routing.py` – API for route preview (origin → destination, multiple travel modes).
- `autocomplete.py` – API providing place suggestions as the user types.
- `chatbot.py` – API for NestFeast Chat (receives a message → calls the chatbot service).

---

#### `backend/app/providers/` – Adapters for external APIs
Providers are isolated for easy substitution and mocking.

- `providers/llm/gemini.py`  
  Wrapper for **Google Gemini** (Flash 2.5) to:
  - Build prompts.
  - Invoke the model in JSON mode.
  - Normalize to `ChatMessage`, `LLMReply`.

- `providers/serpapi/` – uses **SerpAPI** to enrich place information:
  - `client.py` – shared SerpAPI HTTP client (key, base URL, retry).
  - `enrich.py` – calls SerpAPI to fetch additional info/reviews for a Place.
  - `nearby.py` – (optional) supports POI search via SerpAPI.

- `providers/trackasia/` – uses **TrackAsia** for maps and routing:
  - `client.py` – shared HTTP client (base URL, token, timeout, retry).
  - `autocomplete.py` – address/place suggestions from text.
  - `geocode.py` – address → coordinates (geocoding).
  - `reverse.py` – coordinates → address (reverse geocoding).
  - `nearby.py` / `matrix.py` – nearby search + compute ETA/distance among multiple points.
  - `directions.py` – call Directions v2 to obtain the route (polyline) for route preview.
  - `place_detail.py` – fetch details for a single place from TrackAsia.

---

#### `backend/app/schemas/` – Data models / validation (Pydantic)
Defines the shared contract across layers:

- `common.py` – shared types (GeoPoint, PriceLevel, Tag, etc.).
- `location.py` – schemas for origin, coordinates, bounding box, …
- `place.py` – canonical schemas for Place/PlaceCandidate/PlaceEnriched.
- `ranking.py` – schemas for scores, weight configuration, and ranking outputs.
- `chatbot.py` – schemas for chatbot request/response (message, chips, intent, etc.).

---

#### `backend/app/services/` – Business logic / core algorithms
HTTP-agnostic; consumes and returns only Python objects and Pydantic models:

- `intake_origin.py` – origin normalization:
  - parse text,
  - call geocode/autocomplete,
  - resolve ambiguous origins and select the best candidate.
- `poi.py` – destination search logic:
  - call TrackAsia nearby,
  - map to the `PlaceCandidate` schema,
  - (optional) enrich additional information.
- `ranking.py` – core ranking algorithm:
  - compute scores based on rating, distance/ETA, price, etc.,
  - handle fallbacks when fields are missing.
- `ranked.py` – orchestration:
  - intake origin → nearby → matrix → ranking,
  - returns a sorted list of places.
- `routing.py` – route preview logic:
  - call `providers.trackasia.directions`,
  - build geometry + summary for the frontend to render the route.
- `autocomplete.py` – text suggestion logic (combines TrackAsia + filters).
- `chatbot.py` – NestFeast Chat service:
  - analyze intent and call services (ranked, routing, `intake_origin`, …),
  - create quick actions (chips),
  - build prompts and call Gemini via `providers.llm.gemini`.

---

#### `backend/.env`
Contains backend configuration and secrets:

- API keys (Gemini, TrackAsia, SerpAPI, …).
- Timeout, retry, QPS for HTTP clients.
- Other settings (log level, etc.).
- Do not commit publicly.

### 2.2. Frontend (Streamlit)

#### `frontend/`
Directory containing the frontend code (Streamlit UI).

---

#### `frontend/app.py`
Main application file:

- Form UI: origin, radius, transport, type (Cafe/Restaurant/Hotel, …).
- Map area (folium + streamlit-folium) displays markers:
  - origin (green),
  - candidates (orange).
- Buttons: RECOMMENDER, Preview route, quick actions.
- Tab: NestFeast Chat.
- Calls the backend via `backend_client.py`.

---

#### `frontend/backend_client.py`
Lightweight HTTP client to call backend APIs:

- Functions: `intake_origin()`, `search_ranked()`, `route_preview()`, `chatbot_send()`, …
- Normalizes the backend base URL and handles basic errors.

---

#### `frontend/.image/`
Assets used in the Streamlit UI:

- `background.jpg` – application background.
- `logo.jpg` – NestFeast logo.

---

#### `frontend/.streamlit/secrets.toml`
Streamlit-specific configuration (e.g., Streamlit Cloud / Pinggy):

- `BASE_URL` of the backend when deployed.
- Other UI-specific secrets.
