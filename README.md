# Ch-nh-UI

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

# 4. Run BE
```
uvicorn frontend.app.main:app --reload --port 8501 --log-level info
```

# 5. Run FE
```
streamlit run frontend/app.py
```

# 6. Run Pinggy
```
python pinggy_run.py
```

# 7. Get the Pinggy link
From the output of pinggy_run.py, copy the link