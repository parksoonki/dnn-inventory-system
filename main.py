import streamlit as st
import pandas as pd
import re
import io
import time
import os
import shutil
import glob
from datetime import datetime, timedelta
from streamlit_gsheets import GSheetsConnection
from streamlit_option_menu import option_menu
from api_client import get_ecount_inventory, clear_inventory_cache, fetch_realtime_tracking

# 1. 설정 및 백업
st.set_page_config(layout="wide", page_title="DNN 재고 현황")

# 구글 시트 연결 객체 생성
conn = st.connection("gsheets", type=GSheetsConnection)

# =========================================================
# 구글 시트 데이터 읽기/쓰기 (범용)
# =========================================================
def get_data_from_sheet(sheet_name="containers"):
    """
    sheet_name: "containers" (기본 데이터) 또는 "settings" (적정재고)
    """
    try:
        df = conn.read(worksheet=sheet_name, ttl=0)
        
        if sheet_name == "containers":
            required_cols = ["container_no", "arrival_date", "factory_name", "item_name", "qty", "box_qty", "status", "source_file"]
            if df.empty: return pd.DataFrame(columns=required_cols)
            for col in required_cols:
                if col not in df.columns: df[col] = None
            return df
            
        elif sheet_name == "settings":
            required_cols = ["품명", "적정재고"]
            if df.empty: return pd.DataFrame(columns=required_cols)
            for col in required_cols:
                if col not in df.columns: df[col] = None
            return df
            
    except:
        # 에러 시 빈 데이터프레임 반환
        if sheet_name == "containers":
            return pd.DataFrame(columns=["container_no", "arrival_date", "factory_name", "item_name", "qty", "box_qty", "status", "source_file"])
        else:
            return pd.DataFrame(columns=["품명", "적정재고"])

def save_data(df, sheet_name="containers"):
    """
    데이터프레임을 해당 시트에 통째로 덮어쓰기
    """
    conn.update(worksheet=sheet_name, data=df)

# =========================================================
# 자동 동기화 함수
# =========================================================
def try_auto_sync_with_cooldown(minutes=60):
    """
    마지막 동기화 시간으로부터 일정 시간이 지났으면
    진행 중인 화물(해상운송중, 입항완료, 통관완료)을 API로 자동 조회합니다.
    """
    try:
        # 1. 설정 시트에서 마지막 업데이트 시간 확인
        df_set = get_data_from_sheet("settings")
        last_sync_str = None
        
        # '설정키' 컬럼이 없으면 에러 방지를 위해 컬럼 추가
        if '설정키' not in df_set.columns:
            df_set['설정키'] = None
            df_set['설정값'] = None

        # LAST_SYNC 값 찾기
        if not df_set.empty:
            row = df_set[df_set['설정키'] == 'LAST_SYNC']
            if not row.empty:
                last_sync_str = str(row.iloc[0]['설정값'])

        # 동기화 실행 여부 판단
        should_sync = False
        now = datetime.now()
        
        if not last_sync_str or last_sync_str == "None" or last_sync_str == "nan":
            should_sync = True
        else:
            try:
                last_time = datetime.strptime(last_sync_str, "%Y-%m-%d %H:%M:%S")
                # 현재시간 - 마지막시간 > 설정분(60분)
                if (now - last_time) > timedelta(minutes=minutes):
                    should_sync = True
            except:
                should_sync = True 

        # 2. 동기화 실행
        if should_sync:
            # 사용자에게 방해되지 않게 조용히 처리
            df_con = get_data_from_sheet("containers")
            if not df_con.empty:
                # [수정] 통관완료 포함
                target_mask = df_con['status'].isin(['해상운송중', '입항완료', '입고예정', '통관완료'])
                target_indices = df_con[target_mask].index.tolist()
                
                updated_cnt = 0
                if target_indices:
                    # 진행 상황 표시
                    status_area = st.empty()
                    status_area.caption("⏳ 데이터 최신화 중...")
                    
                    for idx in target_indices:
                        con_no = df_con.at[idx, 'container_no']
                        old_status = df_con.at[idx, 'status']
                        
                        # API 조회
                        res = fetch_realtime_tracking(con_no)
                        if res['status'] not in ["오류", "확인불가"]:
                            new_status = res['status']
                            if new_status != old_status:
                                df_con.at[idx, 'status'] = new_status
                                updated_cnt += 1
                        time.sleep(0.1) # 과부하 방지
                    
                    status_area.empty() # 문구 삭제
                    
                    if updated_cnt > 0:
                        save_data(df_con, "containers")
                        st.toast(f"🔄 {updated_cnt}건의 화물 상태가 최신으로 업데이트되었습니다!")
            
            # 3. 시간 갱신 (설정 시트에 기록)
            new_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
            
            # [핵심 수정] pd.concat 경고 방지 로직 적용
            mask = df_set['설정키'] == 'LAST_SYNC'
            if mask.any():
                df_set.loc[mask, '설정값'] = new_time_str
            else:
                new_row = pd.DataFrame([{"품명": None, "적정재고": None, "설정키": "LAST_SYNC", "설정값": new_time_str}])
                
                # df_set이 비어있으면 concat 대신 바로 할당 (경고 방지)
                if df_set.empty:
                    df_set = new_row
                else:
                    # 비어있지 않은 데이터끼리만 합치기
                    df_set = df_set.dropna(how='all', axis=1) 
                    df_set = pd.concat([df_set, new_row], ignore_index=True)

            save_data(df_set, "settings")
            return True
            
    except Exception as e:
        # 에러 나도 앱이 멈추지 않게 조용히 넘어감
        print(f"자동 동기화 실패: {e}")
        return False

# 2. 스타일 CSS
st.markdown("""
<style>
    /* 상단 여백 조정 */
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 5rem;
        padding-left: 1rem !important;  /* 왼쪽 여백 없애기 */
        padding-right: 1rem !important; /* 오른쪽 여백 없애기 */
        max-width: 100% !important;     /* 화면 폭 제한을 풀어서 100% 사용 */
    }

    /* 사이드바 너비 강제 고정 (200px) */
    [data-testid="stSidebar"] {
        min-width: 200px !important;
        max-width: 200px !important;
    }
    
    /* 카드 컨테이너 스타일 */
    div[data-testid="stContainer"] {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 15px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    
    /* [KPI 박스] */
    .kpi-box {
        background-color: #f8f9fa;
        border-radius: 12px;
        padding: 15px;
        text-align: center;
        border: 1px solid #dee2e6;
        height: 110px; 
        display: flex; 
        flex-direction: column; 
        justify-content: center; 
        align-items: center;
    }
    .kpi-title { font-size: 0.9rem; color: #666; font-weight: 600; margin-bottom: 5px; }
    .kpi-value { font-size: 1.8rem; color: #333; font-weight: 800; line-height: 1.2; }
            
    /* [KPI 버튼 전용] 품절 위험 버튼 스타일링 */
    button[kind="primary"] {
        background-color: #ffebee !important;
        border: 1px solid #ffcdd2 !important;
        color: #c62828 !important;
        white-space: pre-wrap !important; /* 텍스트 줄바꿈 허용 */
        height: 110px !important; /* KPI 박스와 높이 맞춤 */
        padding: 0px !important;
    }
    button[kind="primary"]:hover {
        background-color: #ffcdd2 !important;
        border-color: #ef5350 !important;
        color: #b71c1c !important;
    }
    
    /* 버튼 내부 텍스트 폰트 설정 (KPI 박스와 유사하게 맞춤) */
    div.stButton > button[kind="primary"] p {
        font-size: 1.2rem !important; /* 글자 크기 확대 */
        font-weight: 800 !important;   /* 굵게 */
        line-height: 1.4 !important;
        text-align: center !important;
    }
    
    /* [일반 버튼] 조회, 수정, 삭제, 돌아가기 등 (Secondary) */
    button[kind="secondary"] {
        height: auto !important;
        min-height: 42px !important;
        padding-top: 0.5rem !important;
        padding-bottom: 0.5rem !important;
        border: 1px solid #e0e0e0;
    }
            
    /* [무역관리 버튼 사이즈 통일] (다른 버튼/메뉴에 영향 없음) */
    /* 앵커용 마크다운 컨테이너는 레이아웃에 영향을 주지 않도록 숨김 */
    div[data-testid="stMarkdownContainer"]:has(#trade-register-btn),
    div[data-testid="stMarkdownContainer"]:has(#trade-save-btn),
    div[data-testid="stMarkdownContainer"]:has(#trade-delete-btn) {
        display: none !important;
    }

    /* 해당 앵커 "바로 다음" 버튼만 높이를 동일하게 고정 */
    div[data-testid="stMarkdownContainer"]:has(#trade-register-btn) + div[data-testid="stButton"] > button,
    div[data-testid="stMarkdownContainer"]:has(#trade-save-btn) + div[data-testid="stButton"] > button,
    div[data-testid="stMarkdownContainer"]:has(#trade-delete-btn) + div[data-testid="stButton"] > button {
        height: 48px !important;
        min-height: 48px !important;
        box-sizing: border-box !important;
        padding-top: 0.6rem !important;
        padding-bottom: 0.6rem !important;
    }
    

    /* 컨테이너 카드 텍스트 스타일 */
    .card-row-date { font-size: 0.9rem; color: #1565c0; font-weight: 700; margin-bottom: 4px; display: flex; justify-content: space-between; align-items: center; }
    .card-row-title { font-size: 1.1rem; color: #333; font-weight: 800; margin-bottom: 4px; }
    .card-row-sub { font-size: 0.9rem; color: #666; margin-bottom: 8px; }
    .card-row-qty { font-size: 1.0rem; color: #2e7d32; font-weight: 800; background-color: #f1f8e9; padding: 6px; border-radius: 6px; text-align: center; }

    /* 상태 뱃지 */
    .status-badge { background-color: #e3f2fd; color: #1565c0; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; border: 1px solid #bbdefb; }
    .status-shipping { background-color: #fff3e0; color: #ef6c00; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; border: 1px solid #ffe0b2; }
    .status-done { background-color: #e8f5e9; color: #2e7d32; padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; border: 1px solid #c8e6c9; }

</style>
""", unsafe_allow_html=True)

# 3. 세션 초기화
if 'upload_key' not in st.session_state: st.session_state.upload_key = 0
if 'selected_con_main' not in st.session_state: st.session_state.selected_con_main = None
if 'selected_con_trade' not in st.session_state: st.session_state.selected_con_trade = None
if 'show_low_stock' not in st.session_state: st.session_state.show_low_stock = False
if 'last_menu' not in st.session_state: st.session_state.last_menu = "대시보드"

# 4. 사이드바 메뉴
with st.sidebar:
    selected = option_menu(
        "메뉴", 
        ["대시보드", "입고/재고 현황", "무역 관리", "설정"],
        icons=['house', 'box-seam', 'truck', 'gear'],
        menu_icon="cast", 
        default_index=0,
        styles={
            "container": {"padding": "0!important", "background-color": "#fafafa"},
            "icon": {"color": "orange", "font-size": "18px"}, 
            
            # font-size를 16px -> 14px로 줄여서 줄바꿈 방지
            "nav-link": {"font-size": "14px", "text-align": "left", "margin":"0px", "--hover-color": "#eee"},
            
            # background-color를 녹색(#02ab21) -> 파란색(#1565c0)으로 변경 (카드 테두리색과 통일)
            # 검은색 "#333333", 회색은 "#6c757d"로 변경.
            "nav-link-selected": {"background-color": "#1565c0"},
        }
    )
    st.divider()
    st.caption(f"Today: {datetime.now().strftime('%Y-%m-%d')}")

# =========================================================
# 메뉴 이동시 상태 초기화 로직
# =========================================================
if st.session_state.last_menu != selected:
    # 메뉴가 변경되었다면, 대시보드나 다른 페이지의 상세 뷰 상태를 모두 초기화
    st.session_state.show_low_stock = False
    st.session_state.selected_con_main = None
    st.session_state.selected_con_trade = None
    # 현재 메뉴를 저장
    st.session_state.last_menu = selected

menu = selected

# =========================================================
# [유틸] 데이터 파싱 및 가공 함수들
# =========================================================
def format_korean_date(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        return f"{dt.year}.{dt.month}.{dt.day} ({weekdays[dt.weekday()]})"
    except: return date_str

def format_date_with_korean_day(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        return f"{dt.month}월 {dt.day}일 {weekdays[dt.weekday()]}요일 입고"
    except:
        return "날짜 미정"

def render_container_card(con, is_selected=False):
    if con['status'] == "입고완료": badge_cls = "status-done"
    elif con['status'] in ["해상운송중", "입항완료", "통관완료"]: badge_cls = "status-shipping"
    else: badge_cls = "status-badge"
    
    border_style = "2px solid #1565c0" if is_selected else "1px solid #e0e0e0"
    bg_color = "#f0f7ff" if is_selected else "#ffffff"
    check_mark = "✅ " if is_selected else ""

    card_html = f"""
    <div style="border: {border_style}; background-color: {bg_color}; border-radius: 10px; padding: 15px; margin-bottom: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <div class="card-row-date">
            <span>📅 {format_korean_date(con['date'])}</span>
            <span class="{badge_cls}">{con['status']}</span>
        </div>
        <div class="card-row-title">
            {check_mark}{con['con_no']}
        </div>
        <div class="card-row-sub">
            🏭 공장명: {con['factory']}
        </div>
        <div class="card-row-qty">
            {con['total_qty']:,} EA / {con['total_box']:,} BOX
        </div>
    </div>
    """
    return card_html

def get_grouped_containers(hide_old_completed=True):
    try:
        df = get_data_from_sheet()
        if df.empty: return []

        df['arrival_date'] = pd.to_datetime(df['arrival_date'], errors='coerce').dt.strftime('%Y-%m-%d')
        
        grouped = []
        today = datetime.now().date()
        
        for con_no, group in df.groupby("container_no"):
            first = group.iloc[0]
            status = first["status"]
            arr_date_str = str(first["arrival_date"])
            
            if hide_old_completed and status == "입고완료":
                try:
                    arr_date = datetime.strptime(arr_date_str, "%Y-%m-%d").date()
                    if (today - arr_date).days > 7: continue 
                except: pass

            grouped.append({
                "con_no": con_no, 
                "date": arr_date_str, 
                "factory": first["factory_name"],
                "status": status, 
                "total_qty": int(group["qty"].sum()),
                "total_box": int(group["box_qty"].sum()), 
                "item_count": len(group),
                "items": group.rename(columns={"item_name": "item"}).to_dict('records'),
                "ids": group.index.tolist()
            })
            
        grouped.sort(key=lambda x: x["date"], reverse=True) 
        return grouped
    except Exception as e: 
        st.error(f"데이터 로드 중 오류: {e}")
        return []

def parse_multi_container_excel(df):
    containers = []
    header_row_idx = -1
    
    # 1. 헤더 찾기 ("순서", "제품", "EA"가 모두 있는 행)
    for i, row in df.iterrows():
        row_str = " ".join([str(v) for v in row.values]).upper()
        if "순서" in row_str and "제품" in row_str and "EA" in row_str:
            header_row_idx = i
            break
            
    if header_row_idx == -1: return []

    # 2. "제품" 컬럼 위치를 기준으로 각 컨테이너 블록 분리
    num_cols = df.shape[1]
    product_col_indices = []
    for c in range(num_cols):
        val = str(df.iloc[header_row_idx, c]).strip()
        if val == "제품": product_col_indices.append(c)

    for prod_idx in product_col_indices:
        # 2-1. 컨테이너 번호 찾기 (헤더 바로 윗줄 확인)
        con_no = ""
        # 제품 컬럼 기준 앞뒤 4칸 범위 내에서 컨테이너 번호 패턴 검색
        top_row_vals = df.iloc[header_row_idx-1, prod_idx-1:prod_idx+3].values
        for v in top_row_vals:
            s = str(v).strip()
            # 영문+숫자 조합 5자리 이상이고 "입고"란 단어가 없는 것
            if re.search(r'[A-Z0-9]{5,}', s) and "입고" not in s:
                con_no = s; break
        
        if not con_no: continue

        # 2-2. 데이터 추출
        items = []
        meta = {"date": None, "factory": ""}
        sub_df = df.iloc[header_row_idx+1:, prod_idx-1:prod_idx+3]
        
        for _, row in sub_df.iterrows():
            vals = [str(v).strip() for v in row.values]
            row_text = " ".join(vals)
            
            # 날짜 자동 추출 (예: 입고 : 2026년 01월...)
            if "입고" in row_text and ":" in row_text:
                match = re.search(r'(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일', row_text)
                if match: meta["date"] = f"{match.group(1)}-{match.group(2).zfill(2)}-{match.group(3).zfill(2)}"
                continue
            
            # 공장명 추출
            if "공장" in row_text or "SUPPLIER" in row_text:
                meta["factory"] = row_text.replace("공장명", "").replace(":", "").replace("nan", "").strip()
                continue

            # 품목 데이터 (EA가 숫자인 행만)
            try:
                item_name = vals[1]
                ea_str = vals[2].replace(",", "")
                box_str = vals[3].replace(",", "") if len(vals) > 3 else "0"
                
                if item_name and item_name != "nan" and ea_str.isdigit():
                    if any(x in item_name for x in ['합계', '소계', 'TOTAL', 'SUBTOTAL']):
                        continue

                    items.append({
                        "품명": item_name, 
                        "EA": int(ea_str), 
                        "BOX": int(box_str) if box_str.isdigit() else 0
                    })
            except: pass

        if items:
            containers.append({"con_no": con_no, "items": items, "date": meta["date"], "factory": meta["factory"]})
            
    return containers


def parse_single_container_data(df_raw):
    metadata = {"container_no": "", "arrival_date": "", "factory_name": ""}
    header_row_idx = -1
    
    for idx, row in df_raw.iterrows():
        row_values = [str(v).strip() for v in row.values if pd.notna(v) and str(v).strip() != ""]
        row_str = " ".join(row_values)
        row_upper = row_str.upper()

        if not metadata["container_no"]:
            con_match = re.search(r'[A-Z]{4}\s*?[0-9]{6,7}', row_upper)
            if con_match: metadata["container_no"] = con_match.group(0).replace(" ", "")

        if not metadata["arrival_date"]:
            if "202" in row_str:
                match_kr = re.search(r'(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일', row_str)
                if match_kr: 
                    metadata["arrival_date"] = f"{match_kr.group(1)}-{match_kr.group(2).zfill(2)}-{match_kr.group(3).zfill(2)}"
                else:
                    match_std = re.search(r'202\d[-./]\d{1,2}[-./]\d{1,2}', row_str)
                    if match_std:
                         d_str = match_std.group(0).replace('.','-').replace('/','-')
                         try: metadata["arrival_date"] = datetime.strptime(d_str, "%Y-%m-%d").strftime("%Y-%m-%d")
                         except: metadata["arrival_date"] = d_str

        if not metadata["factory_name"]:
            if any(k in row_upper for k in ["ZHEJIANG", "CO.,LTD", "LIMITED", "SUPPLIER"]):
                clean = row_str
                for label in ["공장명", "공장", "SHIPPER", "EXPORTER", "SELLER", "SUPPLIER", "주소", "상호", "수입처"]:
                    clean = clean.replace(label, "")
                clean = clean.replace(":", "").replace('"', "").replace("'", "")
                metadata["factory_name"] = clean.strip()

        if header_row_idx == -1:
            row_nospace = row_upper.replace(" ", "")
            has_item = any(k in row_upper for k in ["제품", "품명", "품목", "ITEM", "DESCRIPTION", "DESC"])
            has_qty = any(k in row_nospace for k in ["수량", "QTY", "EA", "BOX", "PCS", "개수"])
            
            if has_item and has_qty: 
                header_row_idx = idx

    # 컨테이너 번호가 끝까지 안 보이면 -> 임시 번호 생성
    if not metadata["container_no"]:
        # 중복되지 않도록 현재 시간(초단위)을 붙여서 생성
        temp_id = datetime.now().strftime("%H%M%S")
        metadata["container_no"] = f"미지정_{temp_id}"

    if header_row_idx != -1:
        df_items = df_raw.iloc[header_row_idx+1:].copy()
        df_items.columns = df_raw.iloc[header_row_idx]
        df_items.columns = [str(c).upper().replace(' ', '').strip() for c in df_items.columns]
        df_items = df_items.loc[:, ~df_items.columns.str.contains('^UNNAMED')]
        df_items = df_items.dropna(how='all')
    else: 
        df_items = pd.DataFrame()
        
    return metadata, df_items

# =========================================================
# 페이지 1: 메인 대시보드
# =========================================================
if menu == "대시보드":
    try_auto_sync_with_cooldown(minutes=60)
    st.title("🚢 입고 컨테이너 현황")
    
    containers = get_grouped_containers(hide_old_completed=True)
    target_statuses = ['입고예정', '해상운송중', '입항완료', '통관완료']
    incoming_cons = [c for c in containers if c['status'] in target_statuses]

    today_str = datetime.now().strftime("%Y-%m-%d")
    overdue_list = [c for c in incoming_cons if c['date'] < today_str]

    if overdue_list:
        st.error(f"🔥 **입고 지연 경보**: 예정일이 지난 컨테이너가 {len(overdue_list)}건 있습니다!")
        st.divider()

    df_stock = get_ecount_inventory()
    low_stock_cnt = 0
    low_stock_df = pd.DataFrame()

    if not df_stock.empty:
        df_stock['현재고'] = pd.to_numeric(df_stock['현재고'], errors='coerce').fillna(0)
        low_stock_df = df_stock[df_stock['현재고'] < 100].copy()
        low_stock_cnt = len(low_stock_df)
    
    # --- [KPI 박스 영역] ---
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown(f"""<div class="kpi-box"><div class="kpi-title">입고 예정 컨테이너</div><div class="kpi-value">{len(incoming_cons)}<span style="font-size:1rem"> 건</span></div></div>""", unsafe_allow_html=True)
    with col2:
        st.markdown(f"""<div class="kpi-box"><div class="kpi-title">총 입고 예정 수량</div><div class="kpi-value">{sum([c['total_qty'] for c in incoming_cons]):,}<span style="font-size:1rem"> EA</span></div></div>""", unsafe_allow_html=True)
    with col3:
        st.markdown(f"""<div class="kpi-box"><div class="kpi-title">총 입고 예정 박스</div><div class="kpi-value">{sum([c['total_box'] for c in incoming_cons]):,}<span style="font-size:1rem"> BOX</span></div></div>""", unsafe_allow_html=True)
    with col4:
        btn_label = f"⚠️ 품절 위험 품목\n{low_stock_cnt} 건"
        if st.button(btn_label, key="btn_kpi_low", type="primary", use_container_width=True):
            st.session_state.show_low_stock = True
            st.rerun()    

    st.divider()
    
    # --- [상세 리스트 영역] ---
    if st.session_state.show_low_stock:
        c_head, c_btn = st.columns([8.5, 1.5])

        with c_head:
            st.subheader("⚠️ 품절 위험 품목 현황")
        
        with c_btn:
            st.write("") # 줄맞춤용
            if st.button("🔙 돌아가기", type="secondary", use_container_width=True):
                st.session_state.show_low_stock = False
                st.rerun()
        
        # 2. 필터 입력창 디자인 (라벨 옆에 작은 입력창 배치)
        # 비율을 [2, 1, 7] 정도로 주어 입력창을 작게 만들고 왼쪽으로 붙입니다.
        c_label, c_input, c_empty = st.columns([1.2, 1.0, 7.8])
        
        with c_label:
            # 수직 중앙 정렬 느낌을 위해 마진을 살짝 줌
            st.markdown("<div style='padding-top: 10px; font-weight: bold;'>📉 재고 기준 (개 미만) :</div>", unsafe_allow_html=True)
            
        with c_input:
            # label_visibility="collapsed"로 라벨 숨기고, 옆에 배치
            user_threshold = st.number_input("기준", min_value=0, value=100, step=10, label_visibility="collapsed")

        # 3. 데이터 필터링 및 표 출력
        if not df_stock.empty:
            # 숫자형 변환
            df_stock['현재고'] = pd.to_numeric(df_stock['현재고'], errors='coerce').fillna(0)
            
            try:
                df_settings = get_data_from_sheet("settings")
                if not df_settings.empty:
                    # 품명을 기준으로 적정재고 정보를 합칩니다 (VLOOKUP과 비슷)
                    df_stock = pd.merge(df_stock, df_settings, on="품명", how="left")
                    # 적정재고가 없는 품목은 0 또는 기본값으로 채움
                    df_stock["적정재고"] = pd.to_numeric(df_stock["적정재고"], errors='coerce').fillna(0)
                else:
                    df_stock["적정재고"] = 0
            except:
                df_stock["적정재고"] = 0

            # 기준 미만 필터링
            filtered_stock = df_stock[df_stock['현재고'] < user_threshold].copy()
            
            if not filtered_stock.empty:
                # (A) 원하는 컬럼만 선택 [창고, 품목코드, 품명, 현재고]
                # 데이터에 해당 컬럼이 실제로 있는지 확인 후 선택
                avail_cols = [c for c in ['창고', '품목코드', '품명', '현재고', '적정재고'] if c in filtered_stock.columns]
                display_df = filtered_stock[avail_cols].copy()
                
                # (B) '순번' 컬럼 맨 앞에 추가
                display_df.reset_index(drop=True, inplace=True)
                display_df.index += 1
                display_df.reset_index(inplace=True)
                display_df.rename(columns={'index': '순번'}, inplace=True)
                
                # (C) 표 스타일링 (395.0000 -> 395개)
                st.dataframe(
                    display_df,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "순번": st.column_config.NumberColumn("순번", width="small", format="%d"),
                        "창고": st.column_config.TextColumn("창고", width="medium"),
                        "품목코드": st.column_config.TextColumn("품목코드", width="medium"),
                        "품명": st.column_config.TextColumn("품명", width="large"),
                        # [핵심] format="%d개" 로 설정하면 소수점 없이 '개' 단위가 붙습니다.
                        "현재고": st.column_config.NumberColumn("현재고", format="%d개", width="small"),
                        "적정재고": st.column_config.NumberColumn("적정재고", format="%d개", width="small"),
                    }
                )
            else: 
                st.info(f"현재고 {user_threshold}개 미만인 품목이 없습니다.")
        else: 
            st.warning("재고 데이터가 없습니다.")

    else:
        st.subheader("📋 컨테이너 목록")

        if containers:
            cols = st.columns(3)
            for idx, con in enumerate(containers):
                col_idx = idx % 3
                with cols[col_idx]:
                    is_selected = (st.session_state.selected_con_main == con['con_no'])
                    st.markdown(render_container_card(con, is_selected), unsafe_allow_html=True)
                    if st.button("상세보기", key=f"main_v_{con['con_no']}", use_container_width=True, type="secondary"):
                        st.session_state.selected_con_main = con['con_no']
                        st.rerun()

        if st.session_state.selected_con_main:
            sel_con = next((c for c in containers if c['con_no'] == st.session_state.selected_con_main), None)
            if sel_con:
                st.divider()
                st.markdown(f"### 📦 상세 품목 리스트: {sel_con['con_no']}")
                
                detail_df = pd.DataFrame(sel_con['items'])
                detail_df.reset_index(drop=True, inplace=True)
                detail_df.index += 1
                detail_df.reset_index(inplace=True)
                detail_df.rename(columns={'index': '순번'}, inplace=True)
                
                st.dataframe(
                    detail_df,
                    hide_index=True,
                    use_container_width=False,
                    column_order=["순번", "item", "qty", "box_qty"],
                    
                    column_config={
                        "순번": st.column_config.NumberColumn("순번", width="small", format="%d"),
                        "item": st.column_config.TextColumn("품목명", width="large"), # 품목명은 좀 길게
                        "qty": st.column_config.NumberColumn("수량 (EA)", width="small", format="%d"),
                        "box_qty": st.column_config.NumberColumn("박스 (BOX)", width="small", format="%d"),
                    }
                )
        else:
            st.info("표시할 컨테이너가 없습니다.")

# =========================================================
# 페이지 2: 입고/재고 현황
# =========================================================
elif menu == "입고/재고 현황":
    st.title("🔍 재고 현황")
    
    # 비율 조정
    c1, c2, c3, c4 = st.columns([5.5, 1.0, 1.5, 1.0])

    # 1. 검색어 입력
    query = c1.text_input("검색어", placeholder="품명, 코드, 창고 입력", label_visibility="collapsed")

    # 2. 기준 수량
    low_threshold = c2.number_input("기준", min_value=0, value=100, step=10, label_visibility="collapsed", help="기준 수량")
    
    # 3. 필터 체크박스
    show_low_only = c3.checkbox(f"{low_threshold}개 미만 필터", value=False, help="설정된 기준 수량보다 적은 품목만 봅니다.")
    
    # 4. 새로고침
    if c4.button("🔄 재고 새로고침", use_container_width=True, type="secondary"): 
        clear_inventory_cache(); st.rerun()

    df_ecount = get_ecount_inventory()

    # 구글 시트 'settings' 탭에서 불러오기
    df_settings = get_data_from_sheet("settings")

    # [입고 예정 매핑]
    incoming_map = {}
    try:
        df_sheet = get_data_from_sheet("containers")
        # [수정] 통관완료 포함
        target_statuses = ["입고예정", "해상운송중", "입항완료", "통관완료"]
        
        if not df_sheet.empty and 'status' in df_sheet.columns:
            filtered_df = df_sheet[df_sheet['status'].isin(target_statuses)]
            
            for _, row in filtered_df.iterrows():
                key = str(row['item_name']).strip()
                if key not in incoming_map: incoming_map[key] = []
                
                date_val = str(row['arrival_date'])
                try: qty_val = int(row['qty'])
                except: qty_val = 0
                
                incoming_map[key].append({
                    "date": date_val,
                    "qty": qty_val,
                    "status": row['status'],
                    "con_no": row['container_no']
                })
    except Exception as e:
        pass

    if not df_ecount.empty:
        df_ecount['현재고'] = pd.to_numeric(df_ecount['현재고'], errors='coerce').fillna(0)
        
        # [수정됨] 병합 로직 및 들여쓰기 교정
        if not df_settings.empty:
            # 병합 에러 방지를 위해 '품명' 컬럼을 문자열로 강제 통일
            df_ecount['품명'] = df_ecount['품명'].astype(str)
            df_settings['품명'] = df_settings['품명'].astype(str)
            
            df_ecount = pd.merge(df_ecount, df_settings, on="품명", how="left")
        else:
            df_ecount["적정재고"] = 1000 # 설정 데이터가 없으면 기본값

        # [중요] 여기부터는 else 밖으로 나와야 합니다 (들여쓰기 주의)
        # 병합 결과가 NaN인 경우(매칭 안됨) 기본값 1000 채우기
        df_ecount["적정재고"] = pd.to_numeric(df_ecount["적정재고"], errors='coerce').fillna(1000)
        
        # 재고율 계산
        df_ecount["재고율"] = df_ecount["현재고"] / df_ecount["적정재고"]
        
        # 검색 필터 적용
        if query:
            # (주의) 백슬래시 뒤에 공백이 있으면 에러납니다.
            mask = df_ecount["품명"].str.contains(query, regex=False) | \
                   df_ecount["창고"].str.contains(query, regex=False) | \
                   df_ecount["품목코드"].str.contains(query, regex=False)
            df_ecount = df_ecount[mask]
            
        if show_low_only: 
            df_ecount = df_ecount[df_ecount['현재고'] < low_threshold]

        def get_inc_summary(row):
            p = str(row["품명"]).strip()
            if p in incoming_map:
                schedules = sorted(incoming_map[p], key=lambda x: x['date'] if x['date'] else "9999-12-31")
                lines = []
                for sch in schedules:
                    if sch['status'] == "입항완료": status_icon = "⚓"
                    elif sch['status'] == "해상운송중": status_icon = "🚢"
                    elif sch['status'] == "통관완료": status_icon = "✅"
                    else: status_icon = "📅"
                    
                    try: 
                        d_fmt = datetime.strptime(sch['date'], "%Y-%m-%d").strftime("%m/%d") if sch['date'] else "미정"
                    except: d_fmt = "-"
                    
                    lines.append(f"{status_icon} {d_fmt} ({sch['qty']:,} EA)")
                return "\n".join(lines)
            return "-"

        df_ecount["입고요약"] = df_ecount.apply(get_inc_summary, axis=1)

        if not df_ecount.empty:
            df_ecount.reset_index(drop=True, inplace=True); df_ecount.index += 1
            df_ecount.reset_index(inplace=True); df_ecount.rename(columns={'index': 'No'}, inplace=True)
            max_stock = df_ecount['현재고'].max()
            if max_stock == 0 or pd.isna(max_stock): max_stock = 100
        else:
            max_stock = 100

        left_col, spacer, right_col = st.columns([7.2, 0.3, 2.5])

        with left_col:
            df_ecount['qty_display'] = df_ecount['현재고'].apply(lambda x: f"{int(x):,}개")
            
            event = st.dataframe(
                df_ecount, 
                width="stretch",
                hide_index=True, 
                on_select="rerun", 
                selection_mode="single-row", 
                height=600,
                column_order=["No", "창고", "품명", "qty_display", "적정재고", "재고율", "입고요약"],
                column_config={
                    "No": st.column_config.NumberColumn("순번", width="small"),
                    "창고": st.column_config.TextColumn("창고", width="small"),
                    "품명": st.column_config.TextColumn("품명", width="large"),
                    "qty_display": st.column_config.TextColumn("현재고", width="small"),
                    "적정재고": st.column_config.NumberColumn("적정재고", format="%d", width="small"),
                    "재고율": st.column_config.ProgressColumn(
                        "상태", 
                        format="%.0f%%", 
                        min_value=0, 
                        max_value=1, 
                        width="small"
                    ),
                    "입고요약": st.column_config.TextColumn("입고 예정", width="medium")
                }
            )
        with right_col:
            if len(event.selection.rows) > 0:
                selected_row = df_ecount.iloc[event.selection.rows[0]]
                p_name = str(selected_row["품명"]).strip()
                curr_qty = selected_row["현재고"]
                
                st.markdown(f"""
                <div style="border: 1px solid #e0e0e0; border-radius: 10px; padding: 15px; background-color: #ffffff;">
                    <div style="background-color: #e3f2fd; color: #1565c0; padding: 8px 10px; border-radius: 6px; font-weight: bold; margin-bottom: 10px;">
                        📌 {p_name}
                    </div>
                    <div style="font-size: 0.9rem; color: #666;">📦 현재 재고</div>
                    <div style="font-size: 2rem; font-weight: 800; color: #333; line-height: 1.2;">
                        {int(curr_qty):,}<span style="font-size: 1rem; color: #888; font-weight: normal;"> EA</span>
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
                if p_name in incoming_map:
                    schedules = sorted(incoming_map[p_name], key=lambda x: x['date'] if x['date'] else "9999-12-31")
                    total_inc = sum(s['qty'] for s in schedules)
                    
                    st.write("")
                    st.metric("🔜 입고 후 예상", f"{int(curr_qty + total_inc):,} EA", delta=f"+{total_inc:,} EA")
                    st.divider()
                    for sch in schedules:
                        if sch['status'] == "입항완료": icon = "⚓"
                        elif sch['status'] == "해상운송중": icon = "🚢"
                        elif sch['status'] == "통관완료": icon = "✅"
                        else: icon = "📅"
                        
                        with st.container(border=True):
                            st.markdown(f"**{icon} {sch['status']}** ({format_korean_date(sch['date'])})")
                            st.caption(f"{sch['con_no']}")
                            st.markdown(f"**+{sch['qty']:,} EA**")
                else: st.caption("입고 예정 없음")
    else: st.warning("데이터가 없습니다.")

# =========================================================
# 페이지 3: 무역 관리
# =========================================================
elif menu == "무역 관리":
    st.title("🚢 무역 및 컨테이너 관리")
    tab1, tab2, tab3 = st.tabs(["📂 신규 등록", "🛠️ 컨테이너 관리", "💾 백업 관리"])
    
    # -------------------
    # 탭 1: 통합 파일/텍스트 등록
    # -------------------
    with tab1:
        st.markdown("### 📥 엑셀 파일 업로드 또는 텍스트 붙여넣기")
        
        # 1. 입력 소스 받기
        file = st.file_uploader("엑셀 파일 (.xlsx, .xls, .csv)", type=["xlsx", "xls", "csv"], key=f"up_{st.session_state.upload_key}")
        paste_text = st.text_area("또는 엑셀 내용을 여기에 붙여넣으세요 (Ctrl+V)", height=150, placeholder="엑셀에서 복사한 내용을 여기에 붙여넣으면 자동으로 인식")

        if file or paste_text:
            try:
                dfs_to_process = []
                
                if file:
                    if file.name.endswith('.csv'):
                        # CSV는 시트가 없음
                        df_tmp = pd.read_csv(file, header=None)
                        dfs_to_process.append(("CSV 파일", df_tmp))
                    else:
                        xls_dict = pd.read_excel(file, sheet_name=None, header=None)
                        for sheet_name, df_sheet in xls_dict.items():
                            dfs_to_process.append((f"시트: {sheet_name}", df_sheet))
                            
                elif paste_text:
                    # 텍스트 붙여넣기 처리 (탭으로 구분된 엑셀 데이터)
                    df_tmp = pd.read_csv(io.StringIO(paste_text), sep='\t', header=None)
                    dfs_to_process.append(("텍스트 붙여넣기", df_tmp))

                # 3. 모든 시트/데이터 순회하며 컨테이너 파싱
                all_found_containers = []
                
                for source_name, df_raw in dfs_to_process:
                    # 빈 시트 건너뛰기
                    if df_raw.empty: continue
                    
                    # (A) 다중 컨테이너 파싱 시도
                    found = parse_multi_container_excel(df_raw)
                    if found:
                        for c in found: c['source'] = source_name # 출처 기록
                        all_found_containers.extend(found)
                    else:
                        meta, df_items = parse_single_container_data(df_raw)
                        
                        # 유효한 단일 컨테이너인지 확인
                        if meta.get("container_no") or (not df_items.empty and "품명" in df_items.columns):
                            c_items = []
                            if not df_items.empty:
                                cols = df_items.columns
                                c_item = next((c for c in cols if any(k in c for k in ["품명","ITEM","DESC","제품"])), None)
                                c_ea = next((c for c in cols if any(k in c for k in ["EA","수량","QTY","PCS"])), None)
                                c_box = next((c for c in cols if any(k in c for k in ["BOX","박스","CTN"])), None)
                                
                                if c_item and c_ea:
                                    for _, r in df_items.iterrows():
                                        try:
                                            p_name = str(r[c_item]).strip()
                                            if not p_name or p_name.lower() == 'nan' or any(x in p_name for x in ['합계', '소계', 'TOTAL', 'SUBTOTAL']):
                                                continue

                                            ea_val = int(str(r[c_ea]).replace(",",""))
                                            box_val = int(str(r[c_box]).replace(",","")) if c_box else 0
                                            c_items.append({"품명": r[c_item], "EA": ea_val, "BOX": box_val})
                                        except: pass

                            if c_items:
                                all_found_containers.append({
                                    "con_no": meta.get("container_no", "번호미상"),
                                    "items": c_items,
                                    "date": meta.get("arrival_date", datetime.now().strftime("%Y-%m-%d")),
                                    "factory": meta.get("factory_name", ""),
                                    "source": source_name
                                })

                # 4. 결과 처리 및 등록 UI
                if all_found_containers:
                    st.success(f"✅ 총 {len(all_found_containers)}개의 컨테이너를 찾았습니다!")
                    st.divider()
                    
                    final_data_list = []
                    
                    # 각 컨테이너별 편집/확인 카드 생성
                    for idx, con in enumerate(all_found_containers):
                        formatted_date_title = format_date_with_korean_day(con['date'])
                        expander_title = f"📦 {con['con_no']} ({len(con['items'])}개 품목) - {formatted_date_title}"

                        with st.expander(expander_title, expanded=False):
                            c1, c2, c3 = st.columns(3)
                            new_no = c1.text_input("컨테이너 번호", con['con_no'], key=f"no_{idx}")
                            
                            try: d_val = datetime.strptime(con['date'], "%Y-%m-%d").date()
                            except: d_val = datetime.now().date()
                            new_date = c2.date_input("입고예정일", d_val, key=f"date_{idx}")
                            new_fac = c3.text_input("공장명", con['factory'], key=f"fac_{idx}")
                            
                            df_preview = pd.DataFrame(con['items'])
                            st.dataframe(df_preview, width="stretch", hide_index=True, column_config={
                                    "품명": st.column_config.TextColumn("품명", width="medium"),
                                    "EA": st.column_config.NumberColumn("EA", format="%d", width="small"),
                                    "BOX": st.column_config.NumberColumn("BOX", format="%d", width="small")
                                }
                            )
                            
                            final_data_list.append({
                                "no": new_no, "date": new_date, "fac": new_fac, "items": con['items']
                            })
                    
                    st.divider()
                    
                    # 5. 최종 일괄 등록 버튼
                    if st.button("🚀 전체 일괄 등록하기", type="primary", use_container_width=True):
                        try:
                            current_df = get_data_from_sheet()

                            new_rows = []
                            skip_cnt = 0
                            
                            existing_cons = []
                            if not current_df.empty and 'container_no' in current_df.columns:
                                existing_cons = current_df['container_no'].unique().tolist()

                            for data in final_data_list:
                                if not data['no']: continue # 번호 없으면 패스
                                
                                # 중복 체크 (기존에 있으면 스킵)
                                if data['no'] in existing_cons:
                                    st.toast(f"⚠️ {data['no']} : 이미 등록된 번호 (Skip)")
                                    skip_cnt += 1
                                    continue
                                
                                # 저장할 데이터 만들기
                                for item in data['items']:
                                    new_rows.append({
                                        "container_no": data['no'],
                                        "arrival_date": data['date'].strftime("%Y-%m-%d"),
                                        "factory_name": data['fac'],
                                        "item_name": str(item["품명"]),
                                        "qty": int(item["EA"]),
                                        "box_qty": int(item["BOX"]),
                                        "status": "입고예정",
                                        "source_file": "업로드/직접입력"
                                    })
                            
                            if new_rows:
                                new_df = pd.DataFrame(new_rows)
                                # 기존 데이터와 합치기
                                updated_df = pd.concat([current_df, new_df], ignore_index=True)
                                
                                # 구글 시트에 저장
                                save_data(updated_df)
                                
                                st.success(f"✅ 총 {len(new_rows)}개 품목 저장 완료! (중복 제외: {skip_cnt}건)")
                                time.sleep(2); st.session_state.upload_key += 1; st.rerun()
                            else:
                                st.warning("저장할 데이터가 없거나 모두 중복입니다.")
                        except Exception as e:
                            st.error(f"저장 중 오류 발생: {e}")
            except Exception as e:
                st.error(f"파일을 읽거나 분석하는 중 오류가 발생했습니다: {e}")

    # [Tab 2] 관리
    with tab2:
        containers = get_grouped_containers(hide_old_completed=False)
        if containers:
            cols = st.columns(3)
            for idx, con in enumerate(containers):
                with cols[idx % 3]:
                    is_sel = (st.session_state.selected_con_trade == con['con_no'])
                    st.markdown(render_container_card(con, is_sel), unsafe_allow_html=True)

                    if st.button("조회 및 수정", key=f"trade_sel_{con['con_no']}", use_container_width=True, type="secondary"):
                        st.session_state.selected_con_trade = con['con_no']
                        st.rerun()

            st.divider()
            
            if st.session_state.selected_con_trade:
                sel_con = next((c for c in containers if c['con_no'] == st.session_state.selected_con_trade), None)
                if sel_con:
                    st.markdown(f"### 🛠️ 관리: {sel_con['con_no']}")
                    
                    with st.container(border=True):
                        # -----------------------------------------------------
                        # 1. 상단: 기본 정보 수정 (날짜, 상태, 공장) + 통관 조회
                        # -----------------------------------------------------
                        c1, c2, c3, c4 = st.columns(4)
                        
                        try: curr_date_obj = datetime.strptime(str(sel_con['date']), "%Y-%m-%d").date()
                        except: curr_date_obj = datetime.now().date()
                        new_d = c1.date_input("📅 입고예정일", value=curr_date_obj)
                        
                        status_opts = ["입고예정", "해상운송중", "입항완료", "입고완료", "취소"]
                        curr_st = sel_con['status']
                        if curr_st not in status_opts: status_opts.append(curr_st)
                        new_s = c2.selectbox("⚓ 상태", status_opts, index=status_opts.index(curr_st))
                        new_f = c3.text_input("🏭 공장명", value=sel_con['factory'])
                        
                        # [통관 조회 버튼]
                        c4.write("")
                        if c4.button("🔄 통관 조회", use_container_width=True, type="secondary"):
                            res = fetch_realtime_tracking(sel_con['con_no'])
                            if res['status'] not in ["오류", "확인불가"]:
                                try:
                                    df = get_data_from_sheet()
                                    mask = df['container_no'] == sel_con['con_no']
                                    if mask.any():
                                        df.loc[mask, 'status'] = res['status']
                                        save_data(df)
                                        st.toast(f"갱신 완료: {res['status']}")
                                        time.sleep(0.5); st.rerun()
                                    else: st.warning("데이터 없음")
                                except Exception as e: st.error(f"오류: {e}")
                            else: st.error(f"조회 실패: {res['msg']}")

                        # -----------------------------------------------------
                        # 2. 하단: 품목 리스트 수정
                        # -----------------------------------------------------
                        
                        # (1) 전체 데이터 불러오기
                        display_df = pd.DataFrame(sel_con['items'])
                        display_df['db_id'] = sel_con['ids']

                        cols_to_keep = ['item', 'qty', 'box_qty', 'db_id']
                        display_df = display_df[cols_to_keep]

                        # (3) 에디터 설정
                        edited_data = st.data_editor(
                            display_df, 
                            width="stretch", 
                            hide_index=True,
                            column_config={
                                "db_id": None, # ID는 숨김
                                "item": st.column_config.TextColumn("품명", required=True, width="medium"),
                                "qty": st.column_config.NumberColumn("수량 (EA)", required=True, format="%d", width="small"),
                                "box_qty": st.column_config.NumberColumn("박스 (BOX)", required=True, format="%d", width="small")
                            }, 
                            num_rows="dynamic"
                        )
                        
                        st.markdown("")
                        col_save, col_del = st.columns([1, 4])
                        
                        # [저장 버튼] 상단 정보 + 하단 표 동시 저장
                        if col_save.button("💾 변경사항 저장", type="primary", use_container_width=True):
                            try:
                                df = get_data_from_sheet()
                                
                                # (A) 상단 기본 정보 업데이트
                                mask = df['container_no'] == sel_con['con_no']
                                if mask.any():
                                    df.loc[mask, 'arrival_date'] = new_d.strftime("%Y-%m-%d")
                                    df.loc[mask, 'status'] = new_s
                                    df.loc[mask, 'factory_name'] = new_f
                                    
                                    # (B) 하단 품목 정보 업데이트
                                    for _, row in edited_data.iterrows():
                                        idx = row['db_id']
                                        if idx in df.index:
                                            # 구글 시트 컬럼명에 맞춰 대입
                                            df.at[idx, 'item_name'] = str(row['item'])
                                            df.at[idx, 'qty'] = int(row['qty'])
                                            df.at[idx, 'box_qty'] = int(row['box_qty'])
                                    
                                    save_data(df)
                                    st.success("✅ 모든 변경사항이 저장되었습니다.")
                                    time.sleep(1); st.rerun()
                                else: st.error("데이터를 찾을 수 없습니다.")
                            except Exception as e: st.error(f"저장 오류: {e}")
                        
                        st.markdown('<div id="trade-delete-btn"></div>', unsafe_allow_html=True)
                        
                        # [삭제 버튼]
                        if col_del.button("🗑️ 컨테이너 삭제", type="secondary", use_container_width=True):
                            try:
                                df = get_data_from_sheet()
                                df = df[df['container_no'] != sel_con['con_no']]
                                save_data(df)
                                st.toast("삭제 완료"); time.sleep(0.5)
                                st.session_state.selected_con_trade = None; st.rerun()
                            except Exception as e: st.error(f"삭제 오류: {e}")

    # [Tab 3] 백업
    with tab3:
        st.subheader("☁️ 데이터 백업/복원")
        
        # 1. 다운로드
        try:
            df = get_data_from_sheet("containers")
            csv = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button("📥 데이터 다운로드 (CSV)", data=csv, file_name=f"containers_backup_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv")
        except: st.error("데이터 로드 실패")

        st.divider()

        # 2. 복원 (업로드)
        st.warning("🚨 주의: 파일을 업로드하면 기존 데이터가 모두 삭제되고 덮어씌워집니다!")
        up_file = st.file_uploader("복원할 CSV 파일 업로드", type=["csv"])
        
        if up_file and st.button("🚨 데이터 복원 실행 (덮어쓰기)"):
            try:
                df_new = pd.read_csv(up_file)
                # 필수 컬럼 확인
                required = ["container_no", "arrival_date", "factory_name", "item_name", "qty", "box_qty", "status", "source_file"]
                if all(col in df_new.columns for col in required):
                    save_data(df_new, "containers")
                    st.success("✅ 복원 완료! 업데이트되었습니다."); time.sleep(2); st.rerun()
                else:
                    st.error(f"❌ 파일 형식이 올바르지 않습니다. 필수 컬럼: {required}")
            except Exception as e:
                st.error(f"복원 실패: {e}")

# =========================================================
# 페이지 4: 설정 (적정 재고 관리)
# =========================================================
elif menu == "설정":
    st.title("⚙️ 환경 설정")
    
    st.subheader("📊 품목별 적정 재고 설정")
    
    # 구글 시트 'settings' 탭 사용
    df_settings = get_data_from_sheet("settings")

    df_ecount = get_ecount_inventory()
    if not df_ecount.empty:
        current_items = df_ecount["품명"].unique()
        # 설정에 없는 품목 추가
        if not df_settings.empty:
            existing_items = df_settings["품명"].values
            new_items = [item for item in current_items if item not in existing_items]
        else:
            new_items = current_items
            
        if len(new_items) > 0:
            df_new = pd.DataFrame({"품명": new_items, "적정재고": 1000})
            df_settings = pd.concat([df_settings, df_new], ignore_index=True)
    
    edited_settings = st.data_editor(
        df_settings, 
        width="stretch", 
        hide_index=True,
        column_config={
            "품명": st.column_config.TextColumn("품명", disabled=True),
            "적정재고": st.column_config.NumberColumn("적정 재고 (목표)", min_value=1, step=10, format="%d")
        }
    )
    
    if st.button("💾 설정 저장하기", type="primary", use_container_width=True):
        try:
            save_data(edited_settings, "settings")
            st.success("✅ 저장 완료!"); time.sleep(1); st.rerun()
        except Exception as e:
            st.error(f"저장 실패: {e}")