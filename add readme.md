# 📈 SmartBiz Pakistan

A complete business management web app for small and medium businesses in Pakistan, built with
**Streamlit**, **SQLite** and the **Google Gen AI SDK (Gemini)**. All amounts are in **PKR**.

## Features
- 🧾 Sales, with Paid / Udhaar (credit) tracking and automatic stock deduction
- 💸 Expense management with categories
- 📦 Inventory with low-stock and out-of-stock alerts, restock with weighted-average cost
- 👥 Customers: purchase history, outstanding udhaar, receive payment
- 📊 Profit & Loss dashboard (revenue, cost of goods, gross profit, expenses, net profit)
- 📑 Daily, monthly and yearly reports with charts
- ⬇️ CSV export from every table
- 🤖 Gemini AI Business Advisor (English, Urdu, Roman Urdu)
- 💾 Backup and restore, sample data, optional password login

## Files
| File | Purpose |
|------|---------|
| `app.py` | The whole application |
| `requirements.txt` | Python packages |
| `README.md` | This guide |

## Deploy using only your browser

### Step 1: Get a Gemini API key
1. Open https://aistudio.google.com/apikey and sign in with Google.
2. Click **Create API key** and copy it. Keep it private.

### Step 2: Put the code on GitHub
1. Sign in at https://github.com, then click **+ → New repository**.
2. Name it `smartbiz-pakistan`, choose **Public** or **Private**, tick **Add a README file**, and click **Create repository**.
3. Click **Add file → Create new file**, name it `app.py`, paste the code, and click **Commit changes**.
4. Repeat for `requirements.txt`.
5. Open the existing `README.md`, click the pencil icon, paste this README, and commit.
6. Do **not** put your API key in any file.

### Step 3: Deploy on Streamlit Community Cloud
1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click **Create app** (or **New app**) and choose **Deploy a public app from GitHub**.
3. Select your repository, branch `main`, and main file path `app.py`.
4. Click **Advanced settings**:
   - Choose Python **3.12** (or 3.11).
   - In the **Secrets** box, paste:

   ```toml
   GEMINI_API_KEY = "paste-your-gemini-key-here"
   APP_PASSWORD = "choose-a-strong-password"
   # GEMINI_MODEL = "gemini-flash-latest"
   ```
5. Click **Deploy** and wait a few minutes. Your app gets a link like `https://your-app.streamlit.app`.

To change secrets later: open the app → **Manage app → Settings → Secrets**.

### Step 4: First use
1. Log in with `APP_PASSWORD`.
2. Open **⚙️ Settings & Backup → Load Sample Data** to explore, or start adding your own products.
3. Add products → customers → record sales and expenses → view the Dashboard.

## Important notes
- **Data persistence:** Community Cloud storage is temporary. SQLite data can be lost on restart or redeploy.
  Use **Settings & Backup → Download Full Backup** often (for example daily or weekly) and restore it when needed.
  For permanent storage later, move to an external database such as Supabase or Neon.
- **Security:** Set `APP_PASSWORD`, otherwise anyone with the link can see your data. You can also make the
  app private in the Streamlit sharing settings.
- **AI privacy:** The advisor sends only summarised figures (totals, product names, stock levels) to Gemini,
  never customer names or phone numbers. The free Gemini tier has rate limits.
- **Model name:** If Google retires a model, set `GEMINI_MODEL` in Secrets to a current one.
- **Costs:** Streamlit Community Cloud is free. Gemini may be free or paid depending on your Google plan.

## Troubleshooting
| Problem | Fix |
|---------|-----|
| "GEMINI_API_KEY is missing" | Add it in Manage app → Settings → Secrets, then reboot the app |
| "model not found" | Set `GEMINI_MODEL` in Secrets |
| "rate limit / quota" | Wait a minute or upgrade your Gemini quota |
| App shows an install error | Check that `requirements.txt` has exactly the four lines provided |
| Data disappeared | The app restarted, so restore your last backup in Settings |
