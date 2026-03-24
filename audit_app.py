import streamlit as st
import pandas as pd
import io
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
        position: fixed;
        left: 0;
        bottom: 0;
        width: 100%;
        background-color: white;
        color: #888;
        text-align: center;
        padding: 10px;
        font-family: monospace;
        border-top: 1px solid #eee;
    }
    </style>
    """, unsafe_allow_html=True)

# --- 2. CORE LOGIC (CORRECTION-AWARE) ---
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

    # A. REVERSAL LOGIC (Step 1: Identify)
    receipts_with_negative = df.loc[df['_amt_num'] < 0, 'Receipt No'].unique()
    df.loc[df['Receipt No'].isin(receipts_with_negative), 'Reversal_Status'] = 'Reversed'
    # NOTE: We do NOT set Audit_Reason to "Reversal Pair" here anymore. 
    # These are treated as internal system corrections and suppressed from leakage.

    # B. DUPLICATE CHECK (Only for non-reversed transactions)
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

    # C. INCONSISTENCY LOGIC (Step 2: Ignore Reversed Amounts)
    def is_genuinely_inconsistent(amounts):
        # We only care about absolute values for unique price checking
        abs_unique_amts = {abs(float(a)) for a in amounts if a != 0}
        if len(abs_unique_amts) <= 1: return False
        
        small_veh_discount = {2.0, 5.0, 20.0}
        bus_discount = {10.0, 15.0, 40.0}
        
        if abs_unique_amts.issubset(small_veh_discount): return False
        if abs_unique_amts.issubset(bus_discount): return False
        return True

    # IMPORTANT: We group only the "active" (non-reversed) transactions
    inconsistent_regs = []
    # Only analyze plates where the transaction is currently valid (Reversal_Status == 'No')
    valid_txs = df[(df['reg_clean'] != "") & (df['Reversal_Status'] == 'No')]
    grouped = valid_txs.groupby('reg_clean')['_amt_num'].apply(list)
    
    for reg, amounts in grouped.items():
        if is_genuinely_inconsistent(amounts):
            inconsistent_regs.append(reg)
    
    df.loc[(df['reg_clean'].isin(inconsistent_regs)) & (df['Reversal_Status'] == 'No'), 'Inconsistent_Class'] = 'Yes'
    df.loc[(df['Inconsistent_Class'] == 'Yes') & (df['Audit_Reason'] == ""), 'Audit_Reason'] = "Inconsistent Class"

    # D. IRREGULAR AMOUNT CHECK (Only for non-reversed)
    def check_irregular(row):
        if row['Reversal_Status'] == 'Reversed': return "No"
        try:
            amt = abs(float(row['Amount Collected(ZMW)']))
            if amt not in [0, 2, 5, 10, 15, 20, 40, 50, 200, 300, 400, 600, 1000, 3000]: return "Yes"
        except: pass
        return "No"

    df['Irregular_Charge'] = df.apply(check_irregular, axis=1)
    df.loc[(df['Irregular_Charge'] == 'Yes') & (df['Audit_Reason'] == ""), 'Audit_Reason'] = "Irregular Amount"
    
    return df

# --- 3. UI & DASHBOARD ---
st.title("⚖️ E-toll Analysis Solution")

st.sidebar.title("🔍 Audit Filters")
uploaded_file = st.sidebar.file_uploader("Upload E-toll transaction file", type=["xlsx"])

if uploaded_file:
    if 'main_df' not in st.session_state:
        with st.spinner("Analyzing data..."):
            st.session_state.main_df = run_analysis(pd.read_excel(uploaded_file))
    
    df = st.session_state.main_df
    
    # CALCULATIONS
    static_gross = df['_amt_num'].sum()
    static_total_count = len(df)
    
    # Leakage now only counts rows with a valid Audit_Reason 
    # (Reversal pairs no longer have one, so they don't inflate this count)
    static_leakage_df = df[df['Audit_Reason'] != ""]
    static_leakage_total = static_leakage_df['_amt_num'].sum()
    static_leakage_count = len(static_leakage_df)
    static_net = static_gross - static_leakage_total

    st.info("Metrics reflect the TOTAL file. Reversed/Corrected errors are excluded from leakage counts.")
    
    db1, db2, db3 = st.columns(3)
    db1.metric("Overall System Gross", f"K{static_gross:,.2f}", f"{static_total_count} Trans")
    db2.metric("Total Identified Leakage", f"K{static_leakage_total:,.2f}", f"{static_leakage_count} Flags", delta_color="inverse")
    db3.metric("Final Reconciled Revenue", f"K{static_net:,.2f}", "Verified")

    st.markdown("---")

    st.sidebar.subheader("Table Controls")
    search_query = st.sidebar.text_input("Search Vehicle Reg", "").strip().upper()
    plazas = ["All Plazas"] + sorted(df['Plaza'].dropna().unique().tolist())
    sel_plaza = st.sidebar.selectbox("Select Plaza", plazas)
    
    filter_mode = st.sidebar.radio(
        "Data View:", 
        ["Show All Audit Leakages", "Show Entire Dataset", "Inconsistencies Only", "Duplicates Only", "Reversals Only", "Irregular Only"]
    )

    working_df = df.copy()
    if search_query:
        working_df = working_df[working_df['reg_clean'].str.contains(search_query, na=False)]
    if sel_plaza != "All Plazas":
        working_df = working_df[working_df['Plaza'] == sel_plaza]
    
    if filter_mode == "Show All Audit Leakages":
        working_df = working_df[working_df['Audit_Reason'] != ""]
    elif filter_mode == "Inconsistencies Only":
        working_df = working_df[working_df['Inconsistent_Class'] == 'Yes']
    elif filter_mode == "Duplicates Only":
        working_df = working_df[working_df['Duplicate'] == 'Yes']
    elif filter_mode == "Reversals Only":
        working_df = working_df[working_df['Reversal_Status'] == 'Reversed']
    elif filter_mode == "Irregular Only":
        working_df = working_df[working_df['Irregular_Charge'] == 'Yes']

    st.subheader(f"Data Log: {filter_mode}")
    display_cols = [c for c in working_df.columns if c not in ['reg_clean', '_amt_num']]
    st.dataframe(working_df[display_cols], use_container_width=True)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        working_df[display_cols].to_excel(writer, index=False, sheet_name='Audit_Export')
        
    st.download_button(
        label=f"📥 Download {filter_mode} (Excel)",
        data=output.getvalue(),
        file_name=f"NRFA_Export_{datetime.now().strftime('%Y%m%d')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

else:
    st.info("Awaiting file upload...")

st.markdown("""
    <div class="footer">
        Powered by <a href="https://dataamnis.netlify.app/?#" target="_blank" 
        style="color: #006B33; text-decoration: none; font-weight: bold;">DataAmnis</a>
    </div>
    """, unsafe_allow_html=True)
