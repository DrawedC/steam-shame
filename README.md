# Steam Shame 😬

A web app that calculates your Steam library shame score.

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set your Steam API key:**
   
   Windows (PowerShell):
   ```powershell
   $env:STEAM_API_KEY = "your_api_key_here"
   ```
   
   Mac/Linux:
   ```bash
   export STEAM_API_KEY=your_api_key_here
   ```

3. **Run the app:**
   ```bash
   python app.py
   ```

4. **Open in browser:**
   ```
   http://localhost:5000
   ```

## Notes

- Users need their Steam profile AND game details set to **Public**
- Get your API key at: https://steamcommunity.com/dev/apikey

## Project Structure

```
steam-shame/
├── app.py              # Main Flask application
├── requirements.txt    # Python dependencies
├── README.md          # This file
└── templates/
    ├── index.html     # Home page with lookup form
    ├── results.html   # Results page with shame card
    └── error.html     # Error page
```
