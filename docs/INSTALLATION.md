# Windows 11 installation guide

## Requirements

- Windows 11
- Python 3.12 available as `py -3.12`
- VS Code is recommended, not required

## Install

Run in PowerShell from the project directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m database.init_db
```

If PowerShell blocks activation, run `.venv\Scripts\python.exe` directly instead of changing the machine execution policy.

## Run the application

Open two PowerShell terminals, both in the project directory with `.venv` active.

Terminal 1, local API:

```powershell
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Terminal 2, Streamlit UI:

```powershell
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501`. API health is at `http://127.0.0.1:8000/health`; interactive docs are at `http://127.0.0.1:8000/docs`.

The app starts without broker, AI, or Telegram credentials. Those integrations remain unavailable until configured in the local `.env`. Live broker orders are not implemented.

## Upgrade

Back up `data/tradepilot.db` first, then run:

```powershell
python -m database.init_db
```

This applies migrations to the database selected by `DATABASE_URL`. To create a consistent SQLite backup, run `python -m database.init_db --backup`.
