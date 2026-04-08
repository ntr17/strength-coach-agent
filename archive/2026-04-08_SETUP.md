# Setup Guide

Follow these steps in order. Each section is a prerequisite for the next.

---

## 1. Install Dependencies

```bash
cd strength-coach-agent
pip install -r requirements.txt
```

---

## 2. Google Cloud Project

You need a Google Cloud project to access Sheets and Gmail APIs.

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one) — name it anything, e.g. "coach-agent"
3. Enable APIs:
   - Search for **Google Sheets API** → Enable
   - Search for **Gmail API** → Enable
4. Create OAuth credentials:
   - Go to **APIs & Services → Credentials**
   - Click **Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: "Coach Agent"
   - Download the JSON file
   - Save it as `config/credentials.json` (create the `config/` folder first)
5. Configure OAuth consent screen (if prompted):
   - User type: **External** (or Internal if you have Workspace)
   - Add your Gmail address as a test user
   - Scopes: you don't need to add scopes manually

---

## 3. Create Your .env File

Copy the template and fill it in:

```bash
copy .env.example .env
```

Edit `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...          # Get from console.anthropic.com
PROGRAM_SHEET_ID=...                   # See below
MEMORY_SHEET_ID=...                    # See below
GMAIL_FROM=your@gmail.com
GMAIL_TO=your@gmail.com
ATHLETE_NAME=Nacho
CURRENT_WEEK=7                         # Update this each week
PROGRAM_START_DATE=2026-01-13
```

**To get Sheet IDs:** Open the Google Sheet in your browser. The ID is the long string in the URL:
`https://docs.google.com/spreadsheets/d/THIS_IS_THE_ID/edit`

---

## 4. Upload Your Training Program to Google Sheets

1. Open [sheets.google.com](https://sheets.google.com)
2. Import your `programs/strength_30weeks.xlsx` (File → Import)
3. Keep it in Google Drive
4. Copy the Sheet ID into `.env` as `PROGRAM_SHEET_ID`

**Add Date cells to each day:**
In each weekly tab (Week 1 through Week 30), find the DAY header rows and add the date you trained next to them. Example:

| Row | Column A | Column B |
|-----|----------|----------|
| 3   | DAY 1: Squat + Bench Heavy (~50 min) | Date: 2026-02-17 |

You only need to do this going forward — past weeks don't need dates.

**Add a Daily Log tab:**
1. Add a new tab named `Daily Log`
2. Add these headers in row 1 (one per column):
   `Date | Bodyweight (kg) | Steps | Sleep (hrs) | Food Quality (1-10) | Sun (Y/N) | Notes`
3. Fill in from today onward. All fields except Date are optional.

---

## 5. Create a Coach Memory Sheet

1. Create a new empty Google Sheet in Google Drive — name it "Coach Memory"
2. Copy its Sheet ID into `.env` as `MEMORY_SHEET_ID`
3. Run the setup command to create all tabs automatically:

```bash
python src/run_coach.py --setup
```

This creates 7 tabs with the correct structure. After it runs:
- Open the **Athlete Profile** tab and fill in your details
- Open the **Long-Term Goals** tab and add your multi-year goals (not program-specific)

---

## 6. First Auth Run

The first time you run any script that touches Google APIs, it will open your browser to authenticate. This happens once.

```bash
python src/sheets.py
```

A browser window opens → sign in with your Google account → allow access. The token is saved to `config/token.json` and reused automatically from then on.

---

## 7. Validate the Sheet Reader

```bash
python src/sheets.py
```

You should see your goals, current week data, and exercise completion status. Fix any parsing issues before proceeding.

---

## 8. Dry Run

```bash
python src/run_coach.py --dry-run
```

This runs the full pipeline and prints the coaching email to your terminal without sending it. Iterate until the output quality feels right.

To test a specific week:
```bash
python src/run_coach.py --dry-run --week 7
```

---

## 9. Test Email Send

```bash
python src/gmail.py
```

This sends a test email to your Gmail. Check your inbox.

---

## 10. First Real Run

```bash
python src/run_coach.py
```

Check your inbox for the coaching email at the end of the day.

---

## 11. Schedule Daily at 10 PM (Windows Task Scheduler)

1. Open **Task Scheduler** (search in Start menu)
2. Click **Create Basic Task**
3. Name: "Strength Coach Agent"
4. Trigger: **Daily** at **22:00**
5. Action: **Start a program**
   - Program: `C:\Python314\python.exe` (adjust to your Python path)
   - Arguments: `src/run_coach.py`
   - Start in: `C:\Users\Nacho\Documents\Projects\strength-coach-agent`
6. Finish

To find your Python path: `where python` in a terminal.

---

## Updating the Current Week

Each week, update `CURRENT_WEEK` in your `.env` file:

```
CURRENT_WEEK=8
```

Or override for a single run:
```bash
python src/run_coach.py --week 8
```

---

## Troubleshooting

**"File not found: config/credentials.json"**
→ You haven't downloaded the OAuth credentials yet. See Step 2.

**"WorksheetNotFound: Daily Log"**
→ Add the Daily Log tab to your program sheet. See Step 4.

**"WorksheetNotFound: Athlete Profile"**
→ Run `python src/run_coach.py --setup`. See Step 5.

**Email looks generic / missing data**
→ Check that your sheet IDs are correct in `.env`. Run `python src/sheets.py` to debug.

**Token expired errors**
→ Delete `config/token.json` and run any script again to re-authenticate.
