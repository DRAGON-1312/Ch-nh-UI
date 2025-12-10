# NestFeast
## I. Running code
# 1. Create venv
```
python -m venv .venv
```

# 2. Activate venv
## 2.1. For powershell
```
.venv/Scripts/Activate.ps1
```

## 2.2. For Mac/Linux
```
source .venv/bin/activate
```

# 3. Download dependencies
```
pip install -r requirements.txt
```

# 4. Run BE (Create new terminal for BE)
```
cd backend
uvicorn app.main:app --reload --port 8000 --log-level info
```

# 5. Run FE (Create new terminal for FE)
```
cd frontend
python -m streamlit run app.py  
```

# 6. Run Pinggy (Create new terminal for Pinggy)
```
python pinggy_run.py
```

# 7. Get the Pinggy link
From the output of pinggy_run.py, copy the link

## II. Directory structure
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

### 1. Backend (FastAPI)

#### `backend/`
Thư mục chứa toàn bộ mã nguồn backend (API + business logic).

---

#### `backend/app/main.py`
Entrypoint của FastAPI:

- Khởi tạo `FastAPI()`.
- Gắn các router từ `app.api`.
- Cấu hình docs, health check, v.v.

---

#### `backend/app/api/` – HTTP layer (router)
Chỉ chứa các **endpoint** (không chứa logic phức tạp):

- `intake.py` – API nhận origin (text / toạ độ) → gọi service `intake_origin`.
- `nearbySearch.py` – API tìm POI gần origin theo radius, tag.
- `ranked.py` – API recommend địa điểm đã được rank (kết hợp nearby + matrix + ranking).
- `routing.py` – API preview route (origin → destination, nhiều mode di chuyển).
- `autocomplete.py` – API gợi ý địa điểm khi user gõ text.
- `chatbot.py` – API cho NestFeast Chat (nhận message → gọi service chatbot).

---

#### `backend/app/providers/` – Adapter gọi API bên ngoài
Tách riêng từng provider để dễ thay đổi / mock.

- `providers/llm/gemini.py`  
  Wrapper gọi **Google Gemini** (Flash 2.5) để:
  - Tạo prompt.
  - Gọi model ở JSON-mode.
  - Chuẩn hoá dạng `ChatMessage`, `LLMReply`.

- `providers/serpapi/` – dùng **SerpAPI** cho enrich thông tin địa điểm:
  - `client.py` – HTTP client chung cho SerpAPI (key, base URL, retry).
  - `enrich.py` – gọi SerpAPI để lấy thêm info/review cho Place.
  - `nearby.py` – (nếu dùng) hỗ trợ tìm POI bằng SerpAPI.

- `providers/trackasia/` – dùng **TrackAsia** cho map & routing:
  - `client.py` – HTTP client chung (base URL, token, timeout, retry).
  - `autocomplete.py` – gợi ý địa chỉ/địa điểm theo text.
  - `geocode.py` – chuyển địa chỉ → toạ độ.
  - `reverse.py` – chuyển toạ độ → địa chỉ.
  - `nearby.py` / `matrix.py` – tìm địa điểm xung quanh + tính ETA/distance giữa nhiều điểm.
  - `directions.py` – gọi Directions v2 để lấy route (polyline) cho Preview route.
  - `place_detail.py` – lấy chi tiết 1 địa điểm từ TrackAsia.

---

#### `backend/app/schemas/` – Data models / validation (Pydantic)
Định nghĩa “ngôn ngữ chung” cho các layer:

- `common.py` – type dùng chung (GeoPoint, PriceLevel, Tag, v.v.).
- `location.py` – schema cho origin, toạ độ, bounding box…
- `place.py` – schema chuẩn cho Place/PlaceCandidate/PlaceEnriched.
- `ranking.py` – schema cho điểm số, weight config, kết quả ranking.
- `chatbot.py` – schema cho request/response chatbot (message, chips, intent, v.v.).

---

#### `backend/app/services/` – Business logic / core thuật toán
Không phụ thuộc HTTP, chỉ nhận/trả Python object + Pydantic model:

- `intake_origin.py` – chuẩn hoá origin:
  - parse text.
  - gọi geocode/autocomplete.
  - xử lý origin mơ hồ, chọn candidate tốt nhất.
- `poi.py` – logic tìm điểm đến:
  - gọi TrackAsia nearby.
  - map sang schema `PlaceCandidate`.
  - (optional) enrich thêm thông tin.
- `ranking.py` – core thuật toán ranking:
  - tính score dựa trên rating, distance/ETA, price, v.v.
  - xử lý fallback khi thiếu field.
- `ranked.py` – orchestration:
  - intake origin → nearby → matrix → ranking.
  - trả về danh sách địa điểm đã sắp xếp.
- `routing.py` – logic preview route:
  - gọi `providers.trackasia.directions`.
  - build geometry + summary cho FE vẽ route.
- `autocomplete.py` – logic gợi ý text (kết hợp TrackAsia + filter).
- `chatbot.py` – NestFeast Chat service:
  - phân tích intent, gọi các service (ranked, routing, intake_origin,…).
  - tạo quick actions (chips).
  - build prompt & gọi Gemini qua `providers.llm.gemini`.

---

#### `backend/.env`
Chứa cấu hình & secret cho backend:

- API key (Gemini, TrackAsia, SerpAPI…).
- Timeout, retry, QPS cho HTTP client.
- Các setting khác (log level, v.v.).
- **Không commit public.**

### 2. Frontend (Streamlit)

#### `frontend/`
Thư mục chứa code frontend (UI Streamlit).

---

#### `frontend/app.py`
File chính của app:

- Giao diện form: origin, radius, transport, type (Cafe/Restaurant/Hotel…).
- Khu vực map (folium + streamlit-folium) hiển thị marker:
  - origin (màu xanh),
  - candidate (màu cam).
- Nút **RECOMMENDER**, **Preview route**, quick actions.
- Tab **NestFeast Chat**.
- Gọi backend thông qua `backend_client.py`.

---

#### `frontend/backend_client.py`
HTTP client đơn giản để gọi API backend:

- Các hàm: `intake_origin()`, `search_ranked()`, `route_preview()`, `chatbot_send()`, …
- Chuẩn hoá URL backend, handle lỗi cơ bản.

---

#### `frontend/.image/`
Chứa assets dùng trong UI Streamlit:

- `background.jpg` – hình nền app.
- `logo.jpg` – logo NestFeast.

---

#### `frontend/.streamlit/secrets.toml`
File cấu hình riêng cho Streamlit (ví dụ chạy trên Streamlit Cloud / Pinggy):

- `BASE_URL` của backend khi deploy.
- Các secret khác dành riêng cho UI.
