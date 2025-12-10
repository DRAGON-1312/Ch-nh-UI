# Ch-nh-UI

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
