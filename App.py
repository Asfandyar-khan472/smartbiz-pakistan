"""
SmartBiz Pakistan - Business Management App
Streamlit + SQLite + Google Gen AI SDK (Gemini). All amounts in PKR.
"""

import calendar
import hmac
import os
import random
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
    from google import genai
    from google.genai import types

    GENAI_AVAILABLE = True
except Exception:  # SDK missing or broken
    GENAI_AVAILABLE = False

st.set_page_config(page_title="SmartBiz Pakistan", page_icon="📈", layout="wide")

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
APP_NAME = "SmartBiz Pakistan"
CURRENCY = "PKR"
DB_PATH = Path(__file__).resolve().parent / "smartbiz.db"
PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5, no DST)
DEFAULT_MODEL = "gemini-flash-latest"

EXPENSE_CATEGORIES = [
    "Rent", "Electricity", "Gas & Water", "Salaries & Wages", "Transport & Fuel",
    "Marketing & Ads", "Internet & Phone", "Repairs & Maintenance", "Taxes & Fees",
    "Packaging", "Loan / Installment", "Other",
]
PAYMENT_METHODS = ["Cash", "Bank Transfer", "JazzCash", "Easypaisa", "Card", "Cheque"]

st.markdown(
    """
    <style>
    div[data-testid="stMetric"] {
        background: rgba(128,128,128,0.08);
        border: 1px solid rgba(128,128,128,0.22);
        padding: 12px 16px;
        border-radius: 10px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# Generic helpers
# ----------------------------------------------------------------------------
def today() -> date:
    return datetime.now(PKT).date()


def now_iso() -> str:
    return datetime.now(PKT).isoformat(timespec="seconds")


def pkr(x) -> str:
    try:
        return f"{CURRENCY} {float(x):,.0f}"
    except (TypeError, ValueError):
        return f"{CURRENCY} 0"


def fmt_qty(x) -> str:
    try:
        return f"{float(x):,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "0"


def get_secret(name, default=None):
    """Read a value from Streamlit Secrets safely (never crashes)."""
    try:
        value = st.secrets[name]
        if value is None or str(value).strip() == "":
            return default
        return value
    except Exception:
        return default


def flash(kind: str, message: str):
    st.session_state["_flash"] = (kind, message)


def show_flash():
    item = st.session_state.pop("_flash", None)
    if item:
        kind, message = item
        {"success": st.success, "error": st.error, "warning": st.warning}.get(kind, st.info)(message)


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")  # BOM so Excel shows Urdu correctly


def download_csv(df: pd.DataFrame, label: str, filename: str, key: str):
    st.download_button(
        label, csv_bytes(df), file_name=filename, mime="text/csv",
        key=key, disabled=df.empty,
    )


# ----------------------------------------------------------------------------
# Database layer
# ----------------------------------------------------------------------------
def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH), timeout=30)


def _one(conn, sql, params=()):
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return {d[0]: v for d, v in zip(cur.description, row)}


def fetch_one(sql, params=()):
    with closing(get_conn()) as conn:
        return _one(conn, sql, params)


def execute(sql, params=()):
    with closing(get_conn()) as conn:
        with conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid


def query_df(sql, params=()) -> pd.DataFrame:
    with closing(get_conn()) as conn:
        return pd.read_sql_query(sql, conn, params=params)


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    address TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sku TEXT DEFAULT '',
    category TEXT DEFAULT '',
    cost_price REAL NOT NULL DEFAULT 0,
    sell_price REAL NOT NULL DEFAULT 0,
    stock REAL NOT NULL DEFAULT 0,
    reorder_level REAL NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_date TEXT NOT NULL,
    customer_id INTEGER,
    customer_name TEXT NOT NULL DEFAULT 'Walk-in Customer',
    product_id INTEGER,
    product_name TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    unit_cost REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL,
    payment_method TEXT DEFAULT 'Cash',
    payment_status TEXT NOT NULL DEFAULT 'Paid',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_date TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT DEFAULT '',
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date);
"""


def init_db():
    with closing(get_conn()) as conn:
        with conn:
            conn.executescript(SCHEMA)


def record_sale(sale_date, pid, qty, price, cid, method, status):
    """Insert a sale and reduce stock in one atomic transaction."""
    with closing(get_conn()) as conn:
        with conn:
            p = _one(conn, "SELECT * FROM products WHERE id=?", (pid,))
            if p is None:
                raise ValueError("This product no longer exists.")
            if qty > float(p["stock"]) + 1e-9:
                raise ValueError(f"Only {fmt_qty(p['stock'])} units in stock.")
            cname = "Walk-in Customer"
            if cid:
                c = _one(conn, "SELECT name FROM customers WHERE id=?", (cid,))
                if c is None:
                    raise ValueError("This customer no longer exists.")
                cname = c["name"]
            conn.execute(
                """INSERT INTO sales (sale_date, customer_id, customer_name, product_id,
                   product_name, quantity, unit_price, unit_cost, total, payment_method,
                   payment_status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sale_date.isoformat(), cid or None, cname, pid, p["name"], qty, price,
                 float(p["cost_price"]), round(qty * price, 2), method, status, now_iso()),
            )
            conn.execute("UPDATE products SET stock = ROUND(stock - ?, 3) WHERE id=?", (qty, pid))


def delete_sale(sale_id: int, restock: bool):
    with closing(get_conn()) as conn:
        with conn:
            s = _one(conn, "SELECT * FROM sales WHERE id=?", (sale_id,))
            if s is None:
                return
            if restock and s["product_id"]:
                conn.execute(
                    "UPDATE products SET stock = ROUND(stock + ?, 3) WHERE id=?",
                    (s["quantity"], s["product_id"]),
                )
            conn.execute("DELETE FROM sales WHERE id=?", (sale_id,))


# ----------------------------------------------------------------------------
# Analytics helpers
# ----------------------------------------------------------------------------
def date_range_picker(key: str, default: str = "This Month"):
    options = ["Today", "Last 7 Days", "This Month", "Last Month", "Last 30 Days",
               "This Year", "All Time", "Custom"]
    choice = st.selectbox("Period", options, index=options.index(default), key=f"{key}_period")
    t = today()
    if choice == "Today":
        s, e = t, t
    elif choice == "Last 7 Days":
        s, e = t - timedelta(days=6), t
    elif choice == "This Month":
        s, e = t.replace(day=1), t
    elif choice == "Last Month":
        e = t.replace(day=1) - timedelta(days=1)
        s = e.replace(day=1)
    elif choice == "Last 30 Days":
        s, e = t - timedelta(days=29), t
    elif choice == "This Year":
        s, e = date(t.year, 1, 1), t
    elif choice == "All Time":
        s, e = date(2000, 1, 1), date(2099, 12, 31)
    else:
        c1, c2 = st.columns(2)
        s = c1.date_input("From", value=t.replace(day=1), key=f"{key}_from")
        e = c2.date_input("To", value=t, key=f"{key}_to")
        if s > e:
            st.error("'From' date is after 'To' date - they were swapped.")
            s, e = e, s
    return s, e


def financial_summary(s: date, e: date) -> dict:
    a, b = s.isoformat(), e.isoformat()
    sales = fetch_one(
        """SELECT COALESCE(SUM(total),0) AS revenue,
                  COALESCE(SUM(quantity*unit_cost),0) AS cogs,
                  COUNT(*) AS orders
           FROM sales WHERE sale_date BETWEEN ? AND ?""", (a, b))
    exp = fetch_one(
        "SELECT COALESCE(SUM(amount),0) AS expenses FROM expenses WHERE expense_date BETWEEN ? AND ?",
        (a, b))
    rec = fetch_one("SELECT COALESCE(SUM(total),0) AS amt FROM sales WHERE payment_status='Unpaid'")
    inv = fetch_one("SELECT COALESCE(SUM(stock*cost_price),0) AS val FROM products")
    revenue, cogs = float(sales["revenue"]), float(sales["cogs"])
    expenses = float(exp["expenses"])
    gross = revenue - cogs
    net = gross - expenses
    return {
        "revenue": revenue, "cogs": cogs, "gross": gross, "expenses": expenses, "net": net,
        "orders": int(sales["orders"]),
        "avg_order": revenue / sales["orders"] if sales["orders"] else 0.0,
        "gross_margin": (gross / revenue * 100) if revenue else 0.0,
        "net_margin": (net / revenue * 100) if revenue else 0.0,
        "receivables": float(rec["amt"]),
        "stock_value": float(inv["val"]),
    }


def _finish_trend(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("revenue", "cogs", "expenses"):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["gross_profit"] = df["revenue"] - df["cogs"]
    df["net_profit"] = df["gross_profit"] - df["expenses"]
    return df


def daily_trend(s: date, e: date) -> pd.DataFrame:
    a, b = s.isoformat(), e.isoformat()
    sales = query_df(
        """SELECT sale_date AS day, SUM(total) AS revenue, SUM(quantity*unit_cost) AS cogs
           FROM sales WHERE sale_date BETWEEN ? AND ? GROUP BY sale_date""", (a, b))
    exp = query_df(
        """SELECT expense_date AS day, SUM(amount) AS expenses
           FROM expenses WHERE expense_date BETWEEN ? AND ? GROUP BY expense_date""", (a, b))
    df = pd.merge(sales, exp, on="day", how="outer")
    df = df.sort_values("day").reset_index(drop=True)
    return _finish_trend(df)


def monthly_trend(year: int) -> pd.DataFrame:
    y = str(year)
    sales = query_df(
        """SELECT substr(sale_date,1,7) AS month, SUM(total) AS revenue, SUM(quantity*unit_cost) AS cogs
           FROM sales WHERE substr(sale_date,1,4)=? GROUP BY substr(sale_date,1,7)""", (y,))
    exp = query_df(
        """SELECT substr(expense_date,1,7) AS month, SUM(amount) AS expenses
           FROM expenses WHERE substr(expense_date,1,4)=? GROUP BY substr(expense_date,1,7)""", (y,))
    base = pd.DataFrame({"month": [f"{year}-{m:02d}" for m in range(1, 13)]})
    df = base.merge(sales, on="month", how="left").merge(exp, on="month", how="left")
    return _finish_trend(df)


def top_products(s: date, e: date, limit: int = 10) -> pd.DataFrame:
    return query_df(
        """SELECT product_name, SUM(quantity) AS units, SUM(total) AS revenue,
                  SUM(total - quantity*unit_cost) AS profit
           FROM sales WHERE sale_date BETWEEN ? AND ?
           GROUP BY product_name ORDER BY revenue DESC LIMIT ?""",
        (s.isoformat(), e.isoformat(), limit))


def expense_breakdown(s: date, e: date) -> pd.DataFrame:
    return query_df(
        """SELECT category, SUM(amount) AS amount FROM expenses
           WHERE expense_date BETWEEN ? AND ? GROUP BY category ORDER BY amount DESC""",
        (s.isoformat(), e.isoformat()))


def low_stock_df() -> pd.DataFrame:
    return query_df(
        """SELECT name, category, stock, reorder_level FROM products
           WHERE stock <= reorder_level ORDER BY stock ASC, name ASC""")


def trend_chart(df: pd.DataFrame, x_col: str, title: str):
    fig = go.Figure()
    fig.add_bar(x=df[x_col], y=df["revenue"], name="Revenue")
    fig.add_bar(x=df[x_col], y=df["expenses"], name="Expenses")
    fig.add_scatter(x=df[x_col], y=df["net_profit"], name="Net Profit", mode="lines+markers")
    fig.update_layout(
        title=title, barmode="group", yaxis_title=CURRENCY,
        legend=dict(orientation="h", y=-0.2), margin=dict(l=10, r=10, t=50, b=10),
    )
    return fig


def render_summary_metrics(m: dict):
    c = st.columns(5)
    c[0].metric("Revenue", pkr(m["revenue"]))
    c[1].metric("Cost of Goods", pkr(m["cogs"]))
    c[2].metric("Gross Profit", pkr(m["gross"]), f"{m['gross_margin']:.1f}% margin", delta_color="off")
    c[3].metric("Expenses", pkr(m["expenses"]))
    c[4].metric("Net Profit", pkr(m["net"]), f"{m['net_margin']:.1f}% net margin")


# ----------------------------------------------------------------------------
# Authentication (optional - enabled when APP_PASSWORD secret exists)
# ----------------------------------------------------------------------------
def check_login() -> bool:
    pwd = get_secret("APP_PASSWORD")
    if not pwd:
        return True
    if st.session_state.get("authed"):
        return True
    st.title(f"📈 {APP_NAME}")
    st.caption("Please log in to continue.")
    with st.form("login_form"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
    if submitted:
        if hmac.compare_digest(str(entered).encode("utf-8"), str(pwd).encode("utf-8")):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


# ----------------------------------------------------------------------------
# Page: Dashboard
# ----------------------------------------------------------------------------
def page_dashboard():
    st.header("📊 Profit & Loss Dashboard")
    s, e = date_range_picker("dash")
    m = financial_summary(s, e)

    low = low_stock_df()
    if not low.empty:
        names = ", ".join(low["name"].head(8).tolist())
        more = f" and {len(low) - 8} more" if len(low) > 8 else ""
        st.warning(f"⚠️ {len(low)} product(s) low or out of stock: {names}{more}. See the Inventory page.")

    render_summary_metrics(m)
    c = st.columns(4)
    c[0].metric("Orders", f"{m['orders']:,}")
    c[1].metric("Average Order", pkr(m["avg_order"]))
    c[2].metric("Udhaar to Collect", pkr(m["receivables"]), help="All unpaid sales (all time).")
    c[3].metric("Stock Value (at cost)", pkr(m["stock_value"]))

    if m["orders"] == 0 and m["expenses"] == 0:
        st.info("No sales or expenses in this period yet. Record some, or load sample data from Settings & Backup.")
        return

    trend = daily_trend(s, e) if (e - s).days <= 400 else None
    if trend is None:
        st.info("Select a shorter period to see the daily chart.")
    elif not trend.empty:
        st.plotly_chart(trend_chart(trend, "day", "Daily Revenue, Expenses and Net Profit"))

    c1, c2 = st.columns(2)
    top = top_products(s, e, 8)
    with c1:
        st.subheader("🏆 Top Products")
        if top.empty:
            st.caption("No sales in this period.")
        else:
            fig = px.bar(top.sort_values("revenue"), x="revenue", y="product_name",
                         orientation="h", labels={"revenue": f"Revenue ({CURRENCY})", "product_name": ""})
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig)
    with c2:
        st.subheader("💸 Expenses by Category")
        eb = expense_breakdown(s, e)
        if eb.empty:
            st.caption("No expenses in this period.")
        else:
            fig = px.pie(eb, names="category", values="amount", hole=0.45)
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10))
            st.plotly_chart(fig)

    if not low.empty:
        with st.expander("📦 Low-stock details", expanded=False):
            st.dataframe(low.rename(columns={"name": "Product", "category": "Category",
                                             "stock": "In Stock", "reorder_level": "Reorder Level"}),
                         hide_index=True)


# ----------------------------------------------------------------------------
# Page: Sales
# ----------------------------------------------------------------------------
SALES_COLS = {
    "id": "ID", "sale_date": "Date", "customer_name": "Customer", "product_name": "Product",
    "quantity": "Qty", "unit_price": "Unit Price (PKR)", "total": "Total (PKR)",
    "cogs": "Cost (PKR)", "profit": "Profit (PKR)", "payment_method": "Method",
    "payment_status": "Status",
}


def page_sales():
    st.header("🧾 Sales")
    show_flash()
    tab_new, tab_hist = st.tabs(["➕ New Sale", "📜 Sales History"])

    with tab_new:
        products = query_df("SELECT * FROM products ORDER BY name")
        customers = query_df("SELECT id, name, phone FROM customers ORDER BY name")
        if products.empty:
            st.info("Add products in the Inventory page first.")
        else:
            available = products[products["stock"] > 0]
            if available.empty:
                st.warning("All products are out of stock. Restock them in the Inventory page.")
            else:
                labels = {
                    int(r["id"]): f"{r['name']} - stock {fmt_qty(r['stock'])} - {pkr(r['sell_price'])}"
                    for _, r in available.iterrows()
                }
                pid = st.selectbox("Product", list(labels.keys()), format_func=labels.get, key="sale_product")
                prod = available[available["id"] == pid].iloc[0]
                stock = float(prod["stock"])
                c1, c2, c3 = st.columns(3)
                qty = c1.number_input(
                    "Quantity", min_value=0.0, max_value=stock,
                    value=min(1.0, stock), step=1.0, key=f"sale_qty_{pid}_{stock:g}")
                price = c2.number_input(
                    "Unit Price (PKR)", min_value=0.0, value=float(prod["sell_price"]),
                    step=10.0, key=f"sale_price_{pid}_{float(prod['sell_price']):g}")
                sale_date = c3.date_input("Date", value=today(), key="sale_date")

                c4, c5, c6 = st.columns(3)
                cust_labels = {0: "Walk-in Customer"}
                for _, r in customers.iterrows():
                    cust_labels[int(r["id"])] = r["name"] + (f" ({r['phone']})" if r["phone"] else "")
                cid = c4.selectbox("Customer", list(cust_labels.keys()),
                                   format_func=cust_labels.get, key="sale_customer")
                method = c5.selectbox("Payment Method", PAYMENT_METHODS, key="sale_method")
                status_label = c6.radio("Payment Status", ["Paid", "Unpaid (Udhaar)"],
                                        horizontal=True, key="sale_status")

                total = qty * price
                profit = total - qty * float(prod["cost_price"])
                m1, m2 = st.columns(2)
                m1.metric("Sale Total", pkr(total))
                m2.metric("Estimated Profit", pkr(profit))

                if st.button("✅ Record Sale", type="primary", key="btn_record_sale"):
                    if qty <= 0:
                        st.error("Quantity must be greater than zero.")
                    elif status_label.startswith("Unpaid") and cid == 0:
                        st.error("Select a customer for an Udhaar (credit) sale.")
                    else:
                        try:
                            record_sale(sale_date, int(pid), float(qty), float(price), int(cid),
                                        method, "Unpaid" if status_label.startswith("Unpaid") else "Paid")
                            flash("success", f"Sale recorded: {pkr(total)}")
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))

    with tab_hist:
        s, e = date_range_picker("sales")
        df = query_df(
            """SELECT id, sale_date, customer_name, product_name, quantity, unit_price, total,
                      quantity*unit_cost AS cogs, total - quantity*unit_cost AS profit,
                      payment_method, payment_status
               FROM sales WHERE sale_date BETWEEN ? AND ?
               ORDER BY sale_date DESC, id DESC""", (s.isoformat(), e.isoformat()))
        q = st.text_input("🔍 Search by customer or product", key="sales_search")
        if q and not df.empty:
            mask = (df["customer_name"].str.contains(q, case=False, na=False, regex=False)
                    | df["product_name"].str.contains(q, case=False, na=False, regex=False))
            df = df[mask]

        k = st.columns(4)
        k[0].metric("Sales Total", pkr(df["total"].sum() if not df.empty else 0))
        k[1].metric("Profit", pkr(df["profit"].sum() if not df.empty else 0))
        k[2].metric("Transactions", f"{len(df):,}")
        unpaid = df.loc[df["payment_status"] == "Unpaid", "total"].sum() if not df.empty else 0
        k[3].metric("Unpaid in List", pkr(unpaid))

        show = df.round(2).rename(columns=SALES_COLS)
        st.dataframe(show, hide_index=True)
        download_csv(show, "⬇️ Download Sales CSV", f"sales_{s}_{e}.csv", "dl_sales")

        if not df.empty:
            with st.expander("🛠️ Manage a sale (mark paid / delete)"):
                label_map = {
                    int(r["id"]): (f"#{int(r['id'])} | {r['sale_date']} | {r['product_name']} x "
                                   f"{fmt_qty(r['quantity'])} | {pkr(r['total'])} | {r['payment_status']}")
                    for _, r in df.iterrows()
                }
                sid = st.selectbox("Select sale", list(label_map.keys()),
                                   format_func=label_map.get, key="sale_manage_id")
                row = df[df["id"] == sid].iloc[0]
                b1, b2 = st.columns(2)
                with b1:
                    if row["payment_status"] == "Unpaid":
                        if st.button("💵 Mark as Paid", key="btn_mark_paid"):
                            execute("UPDATE sales SET payment_status='Paid' WHERE id=?", (int(sid),))
                            flash("success", "Sale marked as paid.")
                            st.rerun()
                    else:
                        st.caption("This sale is already paid.")
                with b2:
                    restock = st.checkbox("Return quantity to stock", value=True, key="del_restock")
                    if st.button("🗑️ Delete Sale", key="btn_del_sale"):
                        delete_sale(int(sid), restock)
                        flash("success", "Sale deleted.")
                        st.rerun()


# ----------------------------------------------------------------------------
# Page: Expenses
# ----------------------------------------------------------------------------
def page_expenses():
    st.header("💸 Expenses")
    show_flash()
    tab_add, tab_hist = st.tabs(["➕ Add Expense", "📜 Expense History"])

    with tab_add:
        st.caption("Tip: record running costs here (rent, bills, salaries). "
                   "Do not add stock purchases - product cost is already counted automatically on each sale.")
        with st.form("expense_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            exp_date = c1.date_input("Date", value=today())
            category = c2.selectbox("Category", EXPENSE_CATEGORIES)
            amount = c3.number_input("Amount (PKR)", min_value=0.0, value=0.0, step=100.0)
            desc = st.text_input("Description (optional)")
            submitted = st.form_submit_button("Save Expense", type="primary")
        if submitted:
            if amount <= 0:
                st.error("Amount must be greater than zero.")
            else:
                execute(
                    "INSERT INTO expenses (expense_date, category, description, amount, created_at) VALUES (?,?,?,?,?)",
                    (exp_date.isoformat(), category, desc.strip(), float(amount), now_iso()))
                flash("success", f"Expense saved: {pkr(amount)} ({category})")
                st.rerun()

    with tab_hist:
        s, e = date_range_picker("exp")
        df = query_df(
            """SELECT id, expense_date, category, description, amount FROM expenses
               WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date DESC, id DESC""",
            (s.isoformat(), e.isoformat()))
        k = st.columns(3)
        k[0].metric("Total Expenses", pkr(df["amount"].sum() if not df.empty else 0))
        k[1].metric("Entries", f"{len(df):,}")
        if not df.empty:
            k[2].metric("Biggest Category", df.groupby("category")["amount"].sum().idxmax())

        show = df.rename(columns={"id": "ID", "expense_date": "Date", "category": "Category",
                                  "description": "Description", "amount": "Amount (PKR)"})
        st.dataframe(show, hide_index=True)
        download_csv(show, "⬇️ Download Expenses CSV", f"expenses_{s}_{e}.csv", "dl_exp")

        eb = expense_breakdown(s, e)
        if not eb.empty:
            fig = px.pie(eb, names="category", values="amount", hole=0.45, title="Expenses by Category")
            fig.update_layout(margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig)

        if not df.empty:
            with st.expander("🗑️ Delete an expense"):
                label_map = {
                    int(r["id"]): f"#{int(r['id'])} | {r['expense_date']} | {r['category']} | {pkr(r['amount'])}"
                    for _, r in df.iterrows()
                }
                eid = st.selectbox("Select expense", list(label_map.keys()),
                                   format_func=label_map.get, key="exp_delete_id")
                if st.button("Delete Expense", key="btn_del_exp"):
                    execute("DELETE FROM expenses WHERE id=?", (int(eid),))
                    flash("success", "Expense deleted.")
                    st.rerun()


# ----------------------------------------------------------------------------
# Page: Inventory
# ----------------------------------------------------------------------------
def _stock_status(stock, reorder) -> str:
    if stock <= 0:
        return "🔴 Out of stock"
    if stock <= reorder:
        return "🟠 Low stock"
    return "🟢 In stock"


def page_inventory():
    st.header("📦 Inventory")
    show_flash()
    df = query_df("SELECT * FROM products ORDER BY name")
    df["status"] = [_stock_status(s, r) for s, r in zip(df["stock"], df["reorder_level"])]

    k = st.columns(4)
    k[0].metric("Products", f"{len(df):,}")
    k[1].metric("Stock Value (cost)", pkr((df["stock"] * df["cost_price"]).sum() if not df.empty else 0))
    k[2].metric("Retail Value", pkr((df["stock"] * df["sell_price"]).sum() if not df.empty else 0))
    low = df[df["stock"] <= df["reorder_level"]]
    k[3].metric("Low / Out of Stock", f"{len(low):,}")

    if not low.empty:
        out = low[low["stock"] <= 0]
        if not out.empty:
            st.error("🔴 Out of stock: " + ", ".join(out["name"].tolist()))
        warn = low[low["stock"] > 0]
        if not warn.empty:
            st.warning("🟠 Running low: " + ", ".join(
                f"{r['name']} ({fmt_qty(r['stock'])} left)" for _, r in warn.iterrows()))

    tab_list, tab_add, tab_manage = st.tabs(["📋 Stock List", "➕ Add Product", "🛠️ Restock / Edit / Delete"])

    with tab_list:
        c1, c2 = st.columns([3, 1])
        q = c1.text_input("🔍 Search product or category", key="inv_search")
        only_low = c2.checkbox("Low stock only", key="inv_only_low")
        view = df.copy()
        if q and not view.empty:
            mask = (view["name"].str.contains(q, case=False, na=False, regex=False)
                    | view["category"].str.contains(q, case=False, na=False, regex=False))
            view = view[mask]
        if only_low:
            view = view[view["stock"] <= view["reorder_level"]]
        show = view[["id", "name", "sku", "category", "cost_price", "sell_price",
                     "stock", "reorder_level", "status"]].rename(columns={
            "id": "ID", "name": "Product", "sku": "SKU", "category": "Category",
            "cost_price": "Cost (PKR)", "sell_price": "Price (PKR)", "stock": "In Stock",
            "reorder_level": "Reorder Level", "status": "Status"})
        st.dataframe(show, hide_index=True)
        download_csv(show, "⬇️ Download Inventory CSV", f"inventory_{today()}.csv", "dl_inv")

    with tab_add:
        with st.form("add_product_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Product Name *")
            sku = c2.text_input("SKU / Code")
            category = c3.text_input("Category")
            c4, c5, c6, c7 = st.columns(4)
            cost = c4.number_input("Cost Price (PKR)", min_value=0.0, value=0.0, step=10.0)
            sell = c5.number_input("Selling Price (PKR)", min_value=0.0, value=0.0, step=10.0)
            stock = c6.number_input("Opening Stock", min_value=0.0, value=0.0, step=1.0)
            reorder = c7.number_input("Low-stock Alert Level", min_value=0.0, value=5.0, step=1.0)
            submitted = st.form_submit_button("Add Product", type="primary")
        if submitted:
            if not name.strip():
                st.error("Product name is required.")
            else:
                try:
                    execute(
                        """INSERT INTO products (name, sku, category, cost_price, sell_price, stock,
                           reorder_level, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (name.strip(), sku.strip(), category.strip(), float(cost), float(sell),
                         float(stock), float(reorder), now_iso()))
                    msg = f"Product '{name.strip()}' added."
                    if sell < cost:
                        msg += " Note: selling price is below cost."
                    flash("success", msg)
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("A product with this name already exists.")

    with tab_manage:
        if df.empty:
            st.info("No products yet.")
        else:
            labels = {int(r["id"]): f"{r['name']} (stock {fmt_qty(r['stock'])})" for _, r in df.iterrows()}
            pid = st.selectbox("Select product", list(labels.keys()), format_func=labels.get, key="inv_manage_pid")
            p = df[df["id"] == pid].iloc[0]

            st.subheader("📥 Restock")
            with st.form(f"restock_{pid}", clear_on_submit=True):
                r1, r2 = st.columns(2)
                add_qty = r1.number_input("Quantity received", min_value=0.0, value=0.0, step=1.0)
                unit_cost = r2.number_input("Purchase cost per unit (PKR)", min_value=0.0,
                                            value=float(p["cost_price"]), step=10.0)
                st.caption("Cost price is updated using the weighted average of old and new stock.")
                ok = st.form_submit_button("Add Stock")
            if ok:
                if add_qty <= 0:
                    st.error("Enter a quantity greater than zero.")
                else:
                    old_stock = max(float(p["stock"]), 0.0)
                    new_stock = old_stock + add_qty
                    new_cost = (old_stock * float(p["cost_price"]) + add_qty * unit_cost) / new_stock
                    execute("UPDATE products SET stock=?, cost_price=? WHERE id=?",
                            (round(float(p["stock"]) + add_qty, 3), round(new_cost, 2), int(pid)))
                    flash("success", f"Added {fmt_qty(add_qty)} units to '{p['name']}'.")
                    st.rerun()

            st.subheader("✏️ Edit Product")
            with st.form(f"edit_{pid}"):
                c1, c2, c3 = st.columns(3)
                e_name = c1.text_input("Product Name", value=str(p["name"]))
                e_sku = c2.text_input("SKU / Code", value=str(p["sku"] or ""))
                e_cat = c3.text_input("Category", value=str(p["category"] or ""))
                c4, c5, c6, c7 = st.columns(4)
                e_cost = c4.number_input("Cost Price (PKR)", min_value=0.0, value=float(p["cost_price"]), step=10.0)
                e_sell = c5.number_input("Selling Price (PKR)", min_value=0.0, value=float(p["sell_price"]), step=10.0)
                e_stock = c6.number_input("Stock (correct count)", min_value=0.0, value=float(max(p["stock"], 0)), step=1.0)
                e_reorder = c7.number_input("Alert Level", min_value=0.0, value=float(p["reorder_level"]), step=1.0)
                saved = st.form_submit_button("Save Changes", type="primary")
            if saved:
                if not e_name.strip():
                    st.error("Product name is required.")
                else:
                    try:
                        execute(
                            """UPDATE products SET name=?, sku=?, category=?, cost_price=?, sell_price=?,
                               stock=?, reorder_level=? WHERE id=?""",
                            (e_name.strip(), e_sku.strip(), e_cat.strip(), float(e_cost), float(e_sell),
                             float(e_stock), float(e_reorder), int(pid)))
                        flash("success", "Product updated.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Another product already uses this name.")

            st.subheader("🗑️ Delete Product")
            confirm = st.checkbox("I understand past sales will be kept, but this product will be removed.",
                                  key=f"confirm_del_prod_{pid}")
            if st.button("Delete Product", key=f"btn_del_prod_{pid}", disabled=not confirm):
                execute("UPDATE sales SET product_id=NULL WHERE product_id=?", (int(pid),))
                execute("DELETE FROM products WHERE id=?", (int(pid),))
                flash("success", "Product deleted.")
                st.rerun()


# ----------------------------------------------------------------------------
# Page: Customers
# ----------------------------------------------------------------------------
def page_customers():
    st.header("👥 Customers")
    show_flash()
    df = query_df(
        """SELECT c.id, c.name, c.phone, c.email, c.address, c.notes,
                  COUNT(s.id) AS orders,
                  COALESCE(SUM(s.total),0) AS total_purchases,
                  COALESCE(SUM(CASE WHEN s.payment_status='Unpaid' THEN s.total ELSE 0 END),0) AS outstanding
           FROM customers c LEFT JOIN sales s ON s.customer_id = c.id
           GROUP BY c.id ORDER BY c.name""")

    k = st.columns(3)
    k[0].metric("Customers", f"{len(df):,}")
    k[1].metric("Total Udhaar Outstanding", pkr(df["outstanding"].sum() if not df.empty else 0))
    k[2].metric("Customers Owing Money", f"{int((df['outstanding'] > 0).sum()) if not df.empty else 0}")

    tab_list, tab_add, tab_manage = st.tabs(["📋 Customer List", "➕ Add Customer", "🛠️ Details / Edit / Payment"])

    with tab_list:
        q = st.text_input("🔍 Search name or phone", key="cust_search")
        view = df.copy()
        if q and not view.empty:
            mask = (view["name"].str.contains(q, case=False, na=False, regex=False)
                    | view["phone"].str.contains(q, case=False, na=False, regex=False))
            view = view[mask]
        show = view.drop(columns=["notes"]).rename(columns={
            "id": "ID", "name": "Name", "phone": "Phone", "email": "Email", "address": "Address",
            "orders": "Orders", "total_purchases": "Total Purchases (PKR)", "outstanding": "Outstanding (PKR)"})
        st.dataframe(show, hide_index=True)
        download_csv(show, "⬇️ Download Customers CSV", f"customers_{today()}.csv", "dl_cust")

    with tab_add:
        with st.form("add_customer_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            name = c1.text_input("Customer Name *")
            phone = c2.text_input("Phone (e.g. 0300-1234567)")
            c3, c4 = st.columns(2)
            email = c3.text_input("Email")
            address = c4.text_input("Address / City")
            notes = st.text_area("Notes", height=80)
            submitted = st.form_submit_button("Add Customer", type="primary")
        if submitted:
            if not name.strip():
                st.error("Customer name is required.")
            else:
                execute(
                    "INSERT INTO customers (name, phone, email, address, notes, created_at) VALUES (?,?,?,?,?,?)",
                    (name.strip(), phone.strip(), email.strip(), address.strip(), notes.strip(), now_iso()))
                flash("success", f"Customer '{name.strip()}' added.")
                st.rerun()

    with tab_manage:
        if df.empty:
            st.info("No customers yet.")
        else:
            labels = {int(r["id"]): f"{r['name']} - owes {pkr(r['outstanding'])}" for _, r in df.iterrows()}
            cid = st.selectbox("Select customer", list(labels.keys()), format_func=labels.get, key="cust_manage_id")
            c = df[df["id"] == cid].iloc[0]

            m = st.columns(3)
            m[0].metric("Orders", f"{int(c['orders'])}")
            m[1].metric("Total Purchases", pkr(c["total_purchases"]))
            m[2].metric("Outstanding (Udhaar)", pkr(c["outstanding"]))

            if float(c["outstanding"]) > 0:
                if st.button("💵 Receive Payment - mark all unpaid sales as paid", key=f"btn_recv_{cid}"):
                    execute("UPDATE sales SET payment_status='Paid' WHERE customer_id=? AND payment_status='Unpaid'",
                            (int(cid),))
                    flash("success", f"Payment of {pkr(c['outstanding'])} received from {c['name']}.")
                    st.rerun()

            hist = query_df(
                """SELECT sale_date AS Date, product_name AS Product, quantity AS Qty, total AS "Total (PKR)",
                          payment_status AS Status FROM sales WHERE customer_id=? ORDER BY sale_date DESC, id DESC""",
                (int(cid),))
            st.subheader("Purchase History")
            st.dataframe(hist, hide_index=True)
            download_csv(hist, "⬇️ Download This Customer's History", f"customer_{int(cid)}_history.csv", f"dl_ch_{cid}")

            st.subheader("✏️ Edit Customer")
            with st.form(f"edit_cust_{cid}"):
                c1, c2 = st.columns(2)
                e_name = c1.text_input("Name", value=str(c["name"]))
                e_phone = c2.text_input("Phone", value=str(c["phone"] or ""))
                c3, c4 = st.columns(2)
                e_email = c3.text_input("Email", value=str(c["email"] or ""))
                e_addr = c4.text_input("Address", value=str(c["address"] or ""))
                e_notes = st.text_area("Notes", value=str(c["notes"] or ""), height=80)
                saved = st.form_submit_button("Save Changes", type="primary")
            if saved:
                if not e_name.strip():
                    st.error("Name is required.")
                else:
                    execute("UPDATE customers SET name=?, phone=?, email=?, address=?, notes=? WHERE id=?",
                            (e_name.strip(), e_phone.strip(), e_email.strip(), e_addr.strip(),
                             e_notes.strip(), int(cid)))
                    execute("UPDATE sales SET customer_name=? WHERE customer_id=?", (e_name.strip(), int(cid)))
                    flash("success", "Customer updated.")
                    st.rerun()

            st.subheader("🗑️ Delete Customer")
            if float(c["outstanding"]) > 0:
                st.caption("This customer still owes money. Receive payment before deleting.")
            else:
                confirm = st.checkbox("Yes, delete this customer (past sales are kept).", key=f"confirm_del_c_{cid}")
                if st.button("Delete Customer", key=f"btn_del_c_{cid}", disabled=not confirm):
                    execute("UPDATE sales SET customer_id=NULL WHERE customer_id=?", (int(cid),))
                    execute("DELETE FROM customers WHERE id=?", (int(cid),))
                    flash("success", "Customer deleted.")
                    st.rerun()


# ----------------------------------------------------------------------------
# Page: Reports
# ----------------------------------------------------------------------------
def page_reports():
    st.header("📑 Reports")
    tab_day, tab_month, tab_year = st.tabs(["📅 Daily Report", "🗓️ Monthly Report", "📆 Yearly Summary"])

    with tab_day:
        d = st.date_input("Select date", value=today(), key="rep_day")
        m = financial_summary(d, d)
        render_summary_metrics(m)
        sales = query_df(
            """SELECT id AS ID, customer_name AS Customer, product_name AS Product, quantity AS Qty,
                      unit_price AS "Unit Price (PKR)", total AS "Total (PKR)", payment_method AS Method,
                      payment_status AS Status FROM sales WHERE sale_date=? ORDER BY id""", (d.isoformat(),))
        exp = query_df(
            """SELECT id AS ID, category AS Category, description AS Description, amount AS "Amount (PKR)"
               FROM expenses WHERE expense_date=? ORDER BY id""", (d.isoformat(),))
        st.subheader("Sales")
        st.dataframe(sales, hide_index=True)
        st.subheader("Expenses")
        st.dataframe(exp, hide_index=True)
        c1, c2 = st.columns(2)
        with c1:
            download_csv(sales, "⬇️ Sales CSV", f"daily_sales_{d}.csv", "dl_day_sales")
        with c2:
            download_csv(exp, "⬇️ Expenses CSV", f"daily_expenses_{d}.csv", "dl_day_exp")

    with tab_month:
        t = today()
        c1, c2 = st.columns(2)
        year = c1.selectbox("Year", list(range(t.year, t.year - 6, -1)), key="rep_m_year")
        month = c2.selectbox("Month", list(range(1, 13)), index=t.month - 1,
                             format_func=lambda x: calendar.month_name[x], key="rep_m_month")
        s = date(int(year), int(month), 1)
        e = date(int(year), int(month), calendar.monthrange(int(year), int(month))[1])
        m = financial_summary(s, e)
        render_summary_metrics(m)

        trend = daily_trend(s, e)
        if trend.empty:
            st.info("No data for this month.")
        else:
            st.plotly_chart(trend_chart(trend, "day", f"{calendar.month_name[int(month)]} {year} - Daily Performance"))
        c1, c2 = st.columns(2)
        top = top_products(s, e, 10)
        with c1:
            st.subheader("Top Products")
            st.dataframe(top.rename(columns={"product_name": "Product", "units": "Units",
                                             "revenue": "Revenue (PKR)", "profit": "Profit (PKR)"}),
                         hide_index=True)
        with c2:
            st.subheader("Expenses by Category")
            eb = expense_breakdown(s, e)
            st.dataframe(eb.rename(columns={"category": "Category", "amount": "Amount (PKR)"}), hide_index=True)

        st.subheader("Downloads")
        all_sales = query_df("SELECT * FROM sales WHERE sale_date BETWEEN ? AND ? ORDER BY sale_date, id",
                             (s.isoformat(), e.isoformat()))
        all_exp = query_df("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date, id",
                           (s.isoformat(), e.isoformat()))
        d1, d2, d3 = st.columns(3)
        with d1:
            download_csv(trend, "⬇️ Daily Summary CSV", f"monthly_summary_{year}-{int(month):02d}.csv", "dl_m_sum")
        with d2:
            download_csv(all_sales, "⬇️ All Sales CSV", f"sales_{year}-{int(month):02d}.csv", "dl_m_sales")
        with d3:
            download_csv(all_exp, "⬇️ All Expenses CSV", f"expenses_{year}-{int(month):02d}.csv", "dl_m_exp")

    with tab_year:
        t = today()
        year = st.selectbox("Year", list(range(t.year, t.year - 6, -1)), key="rep_y_year")
        df = monthly_trend(int(year))
        if df["revenue"].sum() == 0 and df["expenses"].sum() == 0:
            st.info("No data for this year.")
        else:
            st.plotly_chart(trend_chart(df, "month", f"{year} - Monthly Performance"))
        table = df.rename(columns={
            "month": "Month", "revenue": "Revenue (PKR)", "cogs": "Cost of Goods (PKR)",
            "expenses": "Expenses (PKR)", "gross_profit": "Gross Profit (PKR)", "net_profit": "Net Profit (PKR)"})
        st.dataframe(table.round(0), hide_index=True)
        download_csv(table, "⬇️ Download Yearly Summary CSV", f"yearly_summary_{year}.csv", "dl_year")


# ----------------------------------------------------------------------------
# Page: AI Business Advisor (Gemini)
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are SmartBiz AI Advisor, a practical business consultant for small and medium businesses "
    "in Pakistan (shops, wholesalers, traders, small manufacturers, service businesses). "
    "All money is in Pakistani Rupees (PKR). Use ONLY the business data snapshot below for facts about "
    "the user's business; if data is missing, say so and never invent numbers. "
    "Give concise, actionable advice, prioritised by impact, with specific numbers where possible. "
    "Consider the local context: cash vs udhaar (credit) sales, seasonal demand such as Ramadan, Eid and "
    "wedding season, inflation and supplier price changes, and electricity costs. "
    "For tax or legal questions (FBR, sales tax, income tax) give only general guidance and recommend a "
    "qualified accountant. Reply in the same language the user writes in (English, Urdu or Roman Urdu). "
    "Use short headings and bullet points."
)

QUICK_PROMPTS = {
    "📈 Analyze my business": "Analyze my business health this month compared with last month and tell me the 3 most important things to act on.",
    "💰 Increase profit": "Suggest practical ways to increase my profit based on my products, margins and expenses.",
    "📦 What to restock?": "Which products should I restock first, which are slow-moving, and how should I manage my inventory?",
    "✂️ Reduce expenses": "Review my expenses and suggest where I can cut costs without hurting the business.",
    "🧾 Recover udhaar": "How can I recover my outstanding udhaar (credit) and avoid new bad debts?",
}


def _df_text(df: pd.DataFrame) -> str:
    return "None" if df.empty else df.round(0).to_string(index=False)


def build_business_snapshot() -> str:
    t = today()
    ms = t.replace(day=1)
    prev_end = ms - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    l30_start = t - timedelta(days=29)

    cur = financial_summary(ms, t)
    prev = financial_summary(prev_start, prev_end)
    l30 = financial_summary(l30_start, t)

    def line(label, m):
        return (f"{label}: revenue {m['revenue']:,.0f}, cost of goods {m['cogs']:,.0f}, "
                f"gross profit {m['gross']:,.0f} ({m['gross_margin']:.1f}%), expenses {m['expenses']:,.0f}, "
                f"net profit {m['net']:,.0f} ({m['net_margin']:.1f}%), orders {m['orders']}")

    top = top_products(l30_start, t, 8)
    exp_cat = expense_breakdown(l30_start, t)
    low = low_stock_df().head(10)
    slow = query_df(
        """SELECT name, stock, cost_price FROM products
           WHERE stock > 0 AND id NOT IN (
               SELECT DISTINCT product_id FROM sales WHERE sale_date >= ? AND product_id IS NOT NULL)
           ORDER BY stock*cost_price DESC LIMIT 8""", (l30_start.isoformat(),))
    debt = fetch_one(
        """SELECT COUNT(DISTINCT customer_id) AS n, COALESCE(SUM(total),0) AS amt
           FROM sales WHERE payment_status='Unpaid'""")
    counts = fetch_one("SELECT (SELECT COUNT(*) FROM products) AS p, (SELECT COUNT(*) FROM customers) AS c")

    return "\n".join([
        f"=== BUSINESS DATA SNAPSHOT (as of {t.isoformat()}, currency PKR) ===",
        line(f"This month ({ms} to {t})", cur),
        line(f"Last month ({prev_start} to {prev_end})", prev),
        line("Last 30 days", l30),
        f"Products: {counts['p']}, customers: {counts['c']}, stock value at cost: {cur['stock_value']:,.0f}",
        f"Unpaid udhaar: {debt['amt']:,.0f} across {debt['n']} customers",
        "Top products, last 30 days (units / revenue / profit):",
        _df_text(top),
        "Expenses by category, last 30 days:",
        _df_text(exp_cat),
        "Low or out-of-stock products:",
        _df_text(low),
        "Products with stock but NO sales in last 30 days:",
        _df_text(slow),
    ])


def ask_gemini(question: str, history: list) -> str:
    if not GENAI_AVAILABLE:
        raise RuntimeError("The google-genai package is not installed. Check requirements.txt.")
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it in Streamlit Secrets (see README).")
    model = str(get_secret("GEMINI_MODEL", DEFAULT_MODEL))

    client = genai.Client(api_key=str(api_key))
    contents = []
    for msg in history[-8:]:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=question)]))

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT + "\n\n" + build_business_snapshot(),
            temperature=0.6,
        ),
    )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned an empty response (it may have been blocked). Try rephrasing.")
    return text


def friendly_ai_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "gemini_api_key is missing" in low:
        return msg
    if "api key" in low or "api_key" in low or "permission" in low or "401" in low or "403" in low:
        return "Gemini rejected the API key. Check GEMINI_API_KEY in Streamlit Secrets."
    if "429" in low or "quota" in low or "rate" in low or "resource_exhausted" in low:
        return "Gemini rate limit or free-tier quota reached. Please wait a minute and try again."
    if "404" in low or "not found" in low:
        return ("The Gemini model name was not found. Set GEMINI_MODEL in Secrets "
                "(for example \"gemini-flash-latest\").")
    return f"AI request failed: {msg[:300]}"


def page_ai():
    st.header("🤖 AI Business Advisor")
    st.caption("Powered by Google Gemini. Only summarised business figures are sent - no customer names or phone numbers.")

    if "chat" not in st.session_state:
        st.session_state["chat"] = []

    if not get_secret("GEMINI_API_KEY"):
        st.warning("Gemini is not configured yet. Add GEMINI_API_KEY to Streamlit Secrets (see README).")

    question = None
    cols = st.columns(len(QUICK_PROMPTS))
    for i, (label, prompt_text) in enumerate(QUICK_PROMPTS.items()):
        if cols[i].button(label, key=f"qp_{i}"):
            question = prompt_text

    for msg in st.session_state["chat"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    typed = st.chat_input("Ask about your sales, stock, expenses or growth... (English / Urdu / Roman Urdu)")
    question = question or typed

    if question:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            answer = None
            with st.spinner("Analysing your business data..."):
                try:
                    answer = ask_gemini(question, st.session_state["chat"])
                except Exception as exc:  # show friendly message, never crash the app
                    st.error(friendly_ai_error(exc))
            if answer:
                st.markdown(answer)
                st.session_state["chat"].append({"role": "user", "content": question})
                st.session_state["chat"].append({"role": "assistant", "content": answer})

    if st.session_state["chat"]:
        if st.button("🧹 Clear conversation", key="btn_clear_chat"):
            st.session_state["chat"] = []
            st.rerun()
    st.caption("AI advice is a decision aid, not professional financial or legal advice.")


# ----------------------------------------------------------------------------
# Page: Settings & Backup
# ----------------------------------------------------------------------------
def make_backup_bytes() -> bytes:
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with closing(sqlite3.connect(str(DB_PATH))) as src, closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def restore_backup(data: bytes):
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        with closing(sqlite3.connect(tmp)) as src:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"customers", "products", "sales", "expenses"} <= tables:
                raise ValueError("This file is not a SmartBiz backup.")
            with closing(sqlite3.connect(str(DB_PATH))) as dst:
                src.backup(dst)
        init_db()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def load_sample_data():
    rnd = random.Random(42)
    t = today()
    stamp = now_iso()
    products = [
        ("Basmati Rice 5kg", "RICE-5", "Grocery", 1450, 1700, 120, 20),
        ("Cooking Oil 1L", "OIL-1", "Grocery", 520, 580, 90, 15),
        ("Sugar 1kg", "SUG-1", "Grocery", 150, 170, 100, 25),
        ("Tea Pack 190g", "TEA-190", "Grocery", 290, 340, 60, 12),
        ("Surf Excel 1kg", "DET-1", "Household", 420, 480, 45, 10),
        ("Bath Soap", "SOAP-1", "Household", 85, 110, 70, 20),
        ("Mobile Charger", "CHG-01", "Electronics", 450, 750, 30, 5),
        ("USB Cable", "USB-01", "Electronics", 120, 250, 35, 10),
    ]
    customers = [
        ("Ahmed Khan", "0300-1234567", "Peshawar"),
        ("Bilal Traders", "0333-2345678", "Peshawar"),
        ("Fatima Store", "0321-3456789", "Mardan"),
        ("Gul Muhammad", "0345-4567890", "Charsadda"),
    ]
    with closing(get_conn()) as conn:
        with conn:
            ids = {}
            stock = {}
            for name, sku, cat, cost, sell, stk, rl in products:
                cur = conn.execute(
                    """INSERT INTO products (name, sku, category, cost_price, sell_price, stock,
                       reorder_level, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (name, sku, cat, cost, sell, stk, rl, stamp))
                ids[name] = (cur.lastrowid, cost, sell)
                stock[name] = float(stk)
            cust_ids = []
            for name, phone, addr in customers:
                cur = conn.execute(
                    "INSERT INTO customers (name, phone, email, address, notes, created_at) VALUES (?,?,?,?,?,?)",
                    (name, phone, "", addr, "", stamp))
                cust_ids.append((cur.lastrowid, name))

            for offset in range(29, -1, -1):
                day = t - timedelta(days=offset)
                for _ in range(rnd.randint(2, 6)):
                    pname = rnd.choice(list(ids.keys()))
                    pid, cost, sell = ids[pname]
                    qty = float(rnd.randint(1, 4))
                    if stock[pname] < qty:
                        continue
                    stock[pname] -= qty
                    if rnd.random() < 0.5:
                        cid, cname = cust_ids[rnd.randrange(len(cust_ids))]
                    else:
                        cid, cname = None, "Walk-in Customer"
                    unpaid = cid is not None and rnd.random() < 0.2
                    conn.execute(
                        """INSERT INTO sales (sale_date, customer_id, customer_name, product_id, product_name,
                           quantity, unit_price, unit_cost, total, payment_method, payment_status, created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (day.isoformat(), cid, cname, pid, pname, qty, sell, cost, qty * sell,
                         rnd.choice(["Cash", "Cash", "JazzCash", "Easypaisa", "Bank Transfer"]),
                         "Unpaid" if unpaid else "Paid", stamp))
            for pname, left in stock.items():
                conn.execute("UPDATE products SET stock=? WHERE id=?", (left, ids[pname][0]))
            conn.execute("UPDATE products SET stock=6 WHERE name='Bath Soap'")  # demo low-stock alert

            exp_rows = [
                (t - timedelta(days=25), "Rent", "Shop rent", 35000),
                (t - timedelta(days=20), "Electricity", "WAPDA bill", 8500),
                (t - timedelta(days=15), "Salaries & Wages", "Helper salary", 25000),
                (t - timedelta(days=9), "Transport & Fuel", "Delivery van fuel", 6000),
                (t - timedelta(days=4), "Internet & Phone", "Monthly package", 2500),
            ]
            for d, cat, desc, amt in exp_rows:
                conn.execute(
                    "INSERT INTO expenses (expense_date, category, description, amount, created_at) VALUES (?,?,?,?,?)",
                    (d.isoformat(), cat, desc, amt, stamp))


def page_settings():
    st.header("⚙️ Settings & Backup")
    show_flash()

    st.warning(
        "⚠️ On Streamlit Community Cloud the server disk is temporary. Your data can be reset when the app "
        "restarts, sleeps for a long time or is redeployed. **Download a backup regularly.**")

    st.subheader("💾 Backup & Restore")
    c1, c2 = st.columns(2)
    with c1:
        try:
            st.download_button(
                "⬇️ Download Full Backup (.db)", make_backup_bytes(),
                file_name=f"smartbiz_backup_{today()}.db", mime="application/octet-stream", key="dl_backup")
        except Exception as exc:
            st.error(f"Could not create backup: {exc}")
    with c2:
        up = st.file_uploader("Restore from a backup file", type=["db", "sqlite", "sqlite3"], key="restore_file")
        ok = st.checkbox("I understand this will REPLACE all current data.", key="restore_confirm")
        if st.button("Restore Backup", disabled=not (up and ok), key="btn_restore"):
            try:
                restore_backup(up.getvalue())
                flash("success", "Backup restored successfully.")
                st.rerun()
            except Exception as exc:
                st.error(f"Restore failed: {exc}")

    st.subheader("🧪 Sample Data")
    n_products = fetch_one("SELECT COUNT(*) AS n FROM products")["n"]
    if n_products == 0:
        st.caption("Load demo products, customers, 30 days of sales and expenses to explore the app.")
        if st.button("Load Sample Data", key="btn_sample"):
            load_sample_data()
            flash("success", "Sample data loaded. Open the Dashboard to explore.")
            st.rerun()
    else:
        st.caption("Sample data can only be loaded into an empty database.")

    st.subheader("🔧 System Status")
    st.write(f"- Gemini API key: {'✅ configured' if get_secret('GEMINI_API_KEY') else '❌ missing'}")
    st.write(f"- Gemini SDK: {'✅ installed' if GENAI_AVAILABLE else '❌ not installed'}")
    st.write(f"- Gemini model: `{get_secret('GEMINI_MODEL', DEFAULT_MODEL)}`")
    st.write(f"- Login password: {'✅ enabled' if get_secret('APP_PASSWORD') else '⚠️ not set (anyone with the link can open the app)'}")

    st.subheader("🚨 Danger Zone")
    st.caption("Permanently delete ALL sales, expenses, products and customers.")
    typed = st.text_input("Type DELETE to confirm", key="wipe_confirm")
    if st.button("Delete All Data", disabled=(typed != "DELETE"), key="btn_wipe"):
        with closing(get_conn()) as conn:
            with conn:
                for table in ("sales", "expenses", "products", "customers"):
                    conn.execute(f"DELETE FROM {table}")
                try:
                    conn.execute("DELETE FROM sqlite_sequence")
                except sqlite3.OperationalError:
                    pass
        flash("success", "All data deleted.")
        st.rerun()


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
PAGES = {
    "📊 Dashboard": page_dashboard,
    "🧾 Sales": page_sales,
    "💸 Expenses": page_expenses,
    "📦 Inventory": page_inventory,
    "👥 Customers": page_customers,
    "📑 Reports": page_reports,
    "🤖 AI Advisor": page_ai,
    "⚙️ Settings & Backup": page_settings,
}


def main():
    if not check_login():
        st.stop()
    init_db()

    with st.sidebar:
        st.title(f"📈 {APP_NAME}")
        st.caption(f"Business Manager · {today().strftime('%A, %d %B %Y')}")
        choice = st.radio("Navigation", list(PAGES.keys()), label_visibility="collapsed")
        st.divider()
        n_low = len(low_stock_df())
        if n_low:
            st.error(f"⚠️ {n_low} low-stock item(s)")
        else:
            st.success("✅ Stock levels OK")
        if get_secret("APP_PASSWORD"):
            if st.button("Logout", key="btn_logout"):
                st.session_state["authed"] = False
                st.rerun()

    try:
        PAGES[choice]()
    except Exception as exc:
        st.error("Something went wrong on this page.")
        st.exception(exc)


main()
