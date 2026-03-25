import streamlit as st
import pandas as pd
import io
import matplotlib.pyplot as plt
from datetime import timedelta, datetime

# --- 1. SETTINGS & VISIBILITY ---
st.set_page_config(page_title="E-toll Analysis Solution", page_icon="⚖️", layout="wide")

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { color: black !important; font-size: 1.8rem !important; }
    [data-testid="stMetricLabel"] { color: #444 !important; font-weight: bold !important; }
    div[data-testid="stMetric"] { 
        background-color: #f8f9fa; 
        border: 2px solid #006B33; 
        padding: 15px; 
        border-radius: 10px;
    }
    footer {visibility: hidden;}
    .footer {
        position: fixed; left: 0; bottom: 0; width: 100%;
        background-color: white; color: #888; text-align: center;
        padding: 10px; font-family: monospace; border-top: 1px solid #eee;
    }
    </style>
    """, unsafe_allow_html=True)

# --- 2. CORE AUDIT LOGIC (PRECISION UPDATE) ---
# --- 2. Here lies the brain and engine of this greatness  ---
def clean_vehicle_reg(reg):
    if pd.isna(reg): return ""
    reg = str(reg).strip().upper().replace(" ", "")
    return reg[:-2] if reg.endswith('ZM') else reg

def run_analysis(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = df.columns.str.strip()
    df['DateTime'] = pd.to_datetime(df['Date'], errors='coerce')
    df['reg_clean'] = df['Vehicle Reg.'].apply(clean_vehicle_reg)
    df['Duplicate'] = 'No'
    df['Is_Excess_Duplicate'] = False 
    df['Reversal_Status'] = 'No'
    df['Irregular_Charge'] = 'No'
    df['Inconsistent_Class'] = 'No' 
    df['Audit_Reason'] = ""
    df['_amt_num'] = pd.to_numeric(df['Amount Collected(ZMW)'], errors='coerce').fillna(0)

    # A. REVERSALS (Identify Net-Zero Corrections)
    receipts_with_negative = df.loc[df['_amt_num'] < 0, 'Receipt No'].unique()
    df.loc[df['Receipt No'].isin(receipts_with_negative), 'Reversal_Status'] = 'Reversed'

    # B. DUPLICATES (5-Min Window)
    df = df.sort_values(by=['reg_clean', 'Card Number', 'DateTime'])
    active_df = df[df['Reversal_Status'] == 'No'].copy()
    active_indices = active_df.index.tolist()
    for i in range(len(active_indices) - 1):
        curr_idx, nxt_idx = active_indices[i], active_indices[i+1]
        if (df.at[curr_idx, 'reg_clean'] == df.at[nxt_idx, 'reg_clean'] and 
            df.at[curr_idx, 'Card Number'] == df.at[nxt_idx, 'Card Number'] and 
            df.at[curr_idx, 'DateTime'].date() == df.at[nxt_idx, 'DateTime'].date()):
            diff = abs(df.at[curr_idx, 'DateTime'] - df.at[nxt_idx, 'DateTime'])
            if diff <= timedelta(minutes=5):
                df.at[curr_idx, 'Duplicate'] = 'Yes'
                df.at[nxt_idx, 'Duplicate'] = 'Yes'
                amt1, amt2 = df.at[curr_idx, '_amt_num'], df.at[nxt_idx, '_amt_num']
                if amt1 < amt2:
                    df.at[curr_idx, 'Is_Excess_Duplicate'] = True
                    df.at[curr_idx, 'Audit_Reason'] = f"Duplicate (Under): K{amt1}<K{amt2}"
                elif amt1 > amt2:
                    df.at[curr_idx, 'Is_Excess_Duplicate'] = True
                    df.at[curr_idx, 'Audit_Reason'] = f"Duplicate (Over): K{amt1}>K{amt2}"
                else:
                    df.at[nxt_idx, 'Is_Excess_Duplicate'] = True
                    df.at[nxt_idx, 'Audit_Reason'] = "Duplicate Trans"

    # C. PRECISION INCONSISTENCY (ODD-ONE-OUT LOGIC)
    # This will help give a contextual check for whether a transaction is an odd one out compared to the majority of transactions for that vehicle, while allowing for common discount shifts. For example, if a vehicle usually pays K10 but has a few K15 and K40 transactions, those are likely just discount shifts and not necessarily inconsistent. However, if it has a K2 or K5 transaction, that would be more suspicious as an odd one out.
    small_discount = {2.0, 5.0, 20.0}
    bus_discount = {10.0, 15.0, 40.0}

    valid_txs = df[(df['reg_clean'] != "") & (df['Reversal_Status'] == 'No')].copy()
    if not valid_txs.empty:
        # Get absolute mode (majority price) for each vehicle - this will be the baseline for comparison
        modes = valid_txs.groupby('reg_clean')['_amt_num'].apply(lambda x: abs(x).mode().iloc[0] if not x.mode().empty else 0)
        
        for reg, majority_amt in modes.items():
            if majority_amt == 0: continue
            
            # Find rows for this vehicle that differ from majority
            idx_list = valid_txs[valid_txs['reg_clean'] == reg].index
            for idx in idx_list:
                current_amt = abs(df.at[idx, '_amt_num'])
                if current_amt == majority_amt: continue
                
                # Check if it's just a discount shift (FUD and LUD issues)
                is_discount = (
                    (majority_amt in small_discount and current_amt in small_discount) or
                    (majority_amt in bus_discount and current_amt in bus_discount)
                )
                
                if not is_discount:
                    df.at[idx, 'Inconsistent_Class'] = 'Yes'
                    if df.at[idx, 'Audit_Reason'] == "":
                        df.at[idx, 'Audit_Reason'] = f"Inconsistent Charge: Vehicle usually pays K{int(majority_amt)}"

    # D. EXEMPT AUDIT Not a person but the vehicle. - No way of detecting unregistered ambulances and blood bank vehicles and the BOZ convoy
    official_prefixes = ('AF', 'PS', 'ZP', 'ZAF', 'AB')
    def check_exempt(row):
        is_free = (str(row['Is Exempt']).strip().lower() == 'yes') or (abs(float(row['_amt_num'])) == 0)
        if is_free:
            plate = str(row['reg_clean']).upper()
            if not plate.startswith(official_prefixes): return "Yes"
        return "No"
    df['Exempt_Abuse'] = df.apply(check_exempt, axis=1)
    df.loc[(df['Exempt_Abuse'] == 'Yes') & (df['Audit_Reason'] == ""), 'Audit_Reason'] = "Potential Exempt Abuse"

    # E. IRREGULAR AMOUNT
    valid_fees = [0, 2, 5, 10, 15, 20, 40, 50, 200, 300, 400, 600, 1000, 3000]
    df['Irregular_Charge'] = df.apply(lambda r: "Yes" if abs(r['_amt_num']) not in valid_fees and r['Reversal_Status'] == 'No' else "No", axis=1)
    df.loc[(df['Irregular_Charge'] == 'Yes') & (df['Audit_Reason'] == ""), 'Audit_Reason'] = "Irregular Amount - MCS charge"
    
    return df

# --- 3. UI & ANALYTICS ---
st.title("E-toll Analysis Solution")
st.sidebar.title("🔍 Analysis Filters")


uploaded_file = st.sidebar.file_uploader("Upload E-toll transaction file", type=["xlsx"])

if uploaded_file:
    if 'main_df' not in st.session_state:
        with st.spinner("Analyzing data, please wait..."):
            st.session_state.main_df = run_analysis(pd.read_excel(uploaded_file))
    
    df = st.session_state.main_df
    
    # 1. SIDEBAR CONTROLS
    st.sidebar.subheader("Search & Filter")
    search_query = st.sidebar.text_input("Search Vehicle Reg", "").strip().upper()
    filter_mode = st.sidebar.radio("Data View:", ["Flagged Transactions", "All Data", "Exempt Abuse", "Inconsistencies", "Duplicates", "Irregular Amounts"])

    working_df = df.copy()
    if search_query:
        working_df = working_df[working_df['reg_clean'].str.contains(search_query, na=False)]
    if filter_mode == "Flagged Transactions": working_df = working_df[working_df['Audit_Reason'] != ""]
    elif filter_mode == "Inconsistencies": working_df = working_df[working_df['Inconsistent_Class'] == 'Yes']
    elif filter_mode == "Duplicates": working_df = working_df[working_df['Duplicate'] == 'Yes']
    elif filter_mode == "Exempt Abuse": working_df = working_df[working_df['Exempt_Abuse'] == "Yes"]
    elif filter_mode == "Irregular Amounts": working_df = working_df[working_df['Irregular_Charge'] == 'Yes']

    # 2. METRICS
    gross, leakage_df = df['_amt_num'].sum(), df[df['Audit_Reason'] != ""]
    leakage_total = leakage_df['_amt_num'].sum()
    db1, db2, db3 = st.columns(3)
    db1.metric("System Amount", f"K{gross:,.2f}", f"{len(df)} Trans")
    db2.metric("Flagged Transactions", f"K{leakage_total:,.2f}", f"{len(leakage_df)} Flags", delta_color="inverse")
    db3.metric("Reconciled Revenue", f"K{gross - leakage_total:,.2f}", "Verified")

    st.markdown("---")

    # 3. CENTER: DATA LOG
    st.subheader(f"Detailed Data Log: {filter_mode}")
    display_cols = [c for c in working_df.columns if c not in ['reg_clean', '_amt_num', 'Exempt_Abuse']]
    st.dataframe(working_df[display_cols], use_container_width=True)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        working_df[display_cols].to_excel(writer, index=False, sheet_name='Audit_Export')
    st.download_button("📥 Download Filtered Log (Excel)", output.getvalue(), f"Audit_{datetime.now().strftime('%Y%m%d')}.xlsx")

    st.markdown("---")

    # 4. BOTTOM: ANALYTICS - Visuals and deeper insights for revenue leakage patterns, high-risk vehicles, and operational performance. This will help the team not only identify issues but also understand the underlying patterns and drivers of revenue loss, enabling more strategic interventions.
    st.header("📊 Revenue & Operational Performance")
    
    # Vehicle Class Line Graph + Table
    st.subheader("Vehicle Class: Traffic vs Revenue")
    def map_class(amt):
        a = abs(amt)
        if a in [2, 5, 20]: return "Small Vehicle"
        if a in [10, 15, 40]: return "Light Vehicle"
        if a in [50]: return "Buses"
        if a in [200,400]: return "MHV(2-3 Axles)"
        if a in [300, 600]: return "Heavy (4-5 Axles)"
        if a >= 800: return "Abnormal"
        return "Exempt" if a == 0 else "Other"

    active_only = df[df['Reversal_Status'] == 'No'].copy()
    active_only['Category'] = active_only['_amt_num'].apply(map_class)
    class_stats = active_only.groupby('Category').agg(Traffic_Count=('Receipt No', 'count'), Total_Revenue=('_amt_num', 'sum')).reset_index().sort_values(by='Total_Revenue', ascending=False)

    g_col, t_col = st.columns([2, 1])
    with g_col:
        fig, ax1 = plt.subplots(figsize=(10, 5))
        ax1.plot(class_stats['Category'], class_stats['Traffic_Count'], color='#006B33', marker='o', linewidth=3, label='Traffic')
        ax1.set_ylabel('Traffic', color='#006B33', fontweight='bold')
        ax2 = ax1.twinx()
        ax2.plot(class_stats['Category'], class_stats['Total_Revenue'], color='#FFA500', marker='s', linewidth=3, label='Revenue')
        ax2.set_ylabel('Revenue Million (K)', color='#FFA500', fontweight='bold')
        plt.title('Vehicle Class Traffic vs Revenue', fontsize=12, fontweight='bold')
        st.pyplot(fig)
    with t_col:
        st.write("#### Numerical Summary")
        st.table(class_stats.style.format({"Traffic_Count": "{:,}", "Total_Revenue": "K{:,.2f}"}))

    st.markdown("---")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Flagged Transaction Breakdown")
        reasons = leakage_df['Audit_Reason'].value_counts()
        if not reasons.empty:
            fig_pie, ax_pie = plt.subplots(figsize=(6, 4))
            ax_pie.pie(reasons, labels=reasons.index, autopct='%1.1f%%', startangle=140, colors=['#006B33', '#FF4B4B', '#FFA500', '#007BFF'])
            st.pyplot(fig_pie)
    with c2:
        st.subheader("Toll Collector Risk Ranking")
        cash_s = df.groupby('Cashier').agg(Total=('Receipt No', 'count'), Flags=('Audit_Reason', lambda x: (x != "").sum())).reset_index()
        cash_s['Risk'] = (cash_s['Flags'] / cash_s['Total']) * 100
        st.table(cash_s.sort_values(by='Flags', ascending=False).head(10).style.format({"Risk": "{:.1f}%"}))

else:
    st.info("Awaiting file upload for analysis...")

st.markdown("""<div class="footer">Powered by <a href="https://dataamnis.netlify.app/?#" target="_blank" style="color: #006B33; text-decoration: none; font-weight: bold;">DataAmnis</a></div>""", unsafe_allow_html=True)