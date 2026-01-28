import requests
import pandas as pd
import streamlit as st
import random
import time
from datetime import datetime
import xml.etree.ElementTree as ET
import re


# ---------------------------------------------------------
# 1. 이카운트 및 유니패스 설정 로드
# ---------------------------------------------------------
try:
    SECRETS = st.secrets["ecount"]
    COM_CODE = str(SECRETS["com_code"])
    USER_ID = SECRETS["user_id"]
    API_KEY = SECRETS["api_key"]
    WH_CODE = SECRETS.get("warehouse_code", "")

    UNIPASS_KEY = st.secrets["unipass"]["api_key"]

except Exception:
    st.error("secrets.toml 설정 오류")
    st.stop()

# ---------------------------------------------------------
# 2. 주소 설정
# ---------------------------------------------------------
ZONE = "CA"
BASE_URL = f"https://oapi{ZONE}.ecount.com"

LOGIN_PATH = "/OAPI/V2/OAPILogin"
INVENTORY_PATH = "/OAPI/V2/InventoryBalance/GetListInventoryBalanceStatusByLocation"

# ---------------------------------------------------------
# 3. 이카운트 재고 조회
# ---------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner="데이터 조회 및 변환 중...")
def get_ecount_inventory():
    empty_df = pd.DataFrame(columns=["품목코드", "품명", "규격", "창고", "현재고"])
    session = requests.Session()

    try:
        # --- [STEP 1] 로그인 ---
        login_url = f"{BASE_URL}{LOGIN_PATH}"
        payload = {
            "COM_CODE": COM_CODE, "USER_ID": USER_ID, "API_CERT_KEY": API_KEY,
            "LAN_TYPE": "ko-KR", "ZONE": ZONE
        }
        headers = {'Content-Type': 'application/json'}
        
        # [안정성] 타임아웃 추가 (10초)
        res = session.post(login_url, json=payload, headers=headers, timeout=10)
        result = res.json()
        
        if str(result.get("Status")) != "200":
            st.error(f"로그인 실패: {result.get('Errors')}")
            return empty_df

        session_id = None
        if "Data" in result and isinstance(result["Data"], dict):
            session_id = result["Data"].get("SESSION_ID")
        if not session_id:
            session_id = res.cookies.get("ec_req_sid")
        if not session_id and "Data" in result and "Datas" in result["Data"]:
             session_id = result["Data"]["Datas"].get("SESSION_ID")

        # --- [STEP 2] 재고 조회 ---
        today_str = datetime.now().strftime("%Y%m%d")
        inv_url = f"{BASE_URL}{INVENTORY_PATH}?SESSION_ID={session_id}"
        inv_payload = {
            "BASE_DATE": today_str, "WH_CD": WH_CODE, "PROD_CD": "", "ZERO_FLAG": "N"
        }
        
        res = session.post(inv_url, json=inv_payload, headers=headers, timeout=10)
        result = res.json()
        
        if str(result.get("Status")) != "200":
            return empty_df
            
        items = result["Data"]["Result"]
        df = pd.DataFrame(items)
        if df.empty: return empty_df

        if "PROD_CD" in df.columns:
            df = df[df["PROD_CD"].str.startswith("DNN-26")]
        if df.empty: return empty_df

        # 데이터 정리
        df_clean = pd.DataFrame()
        df_clean["품목코드"] = df.get("PROD_CD", "")
        
        if "PROD_SIZE_DES" in df.columns: df_clean["품명"] = df["PROD_SIZE_DES"]
        elif "SIZE_DES" in df.columns: df_clean["품명"] = df["SIZE_DES"]
        elif "STND_NM" in df.columns: df_clean["품명"] = df["STND_NM"]
        else: df_clean["품명"] = df.get("PROD_DES", "")

        if "WH_DES" in df.columns: df_clean["창고"] = df["WH_DES"]
        elif "WH_NM" in df.columns: df_clean["창고"] = df["WH_NM"]
        else: df_clean["창고"] = "알수없음"

        df_clean["현재고"] = pd.to_numeric(df.get("BAL_QTY", 0), errors='coerce').fillna(0)
        
        return df_clean

    except Exception as e:
        st.error(f"이카운트 데이터 처리 오류: {e}")
        return empty_df

def clear_inventory_cache():
    get_ecount_inventory.clear()


# ---------------------------------------------------------
# 컨테이너 추적 API (관세청/해수부 연동용)
# ---------------------------------------------------------
def fetch_realtime_tracking(input_no):
    """
    [H.B/L 입력 전용 해결책]
    사용자가 H.B/L 번호만 입력했을 때, API가 요구하는 'B/L발행년도'를 
    2024년~2027년까지 전부 대입해서 무조건 찾아냅니다.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # API 호출 헬퍼
    def call_api(params):
        try:
            # 필수 파라미터만 남기기
            p = {k: v for k, v in params.items() if v}
            p["crkyCn"] = UNIPASS_KEY
            
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            res = requests.get(url, params=p, timeout=3) # 속도를 위해 타임아웃 짧게
            
            if res.status_code != 200: return False, None
            
            # XML 파싱 (네임스페이스 제거)
            root = ET.fromstring(res.content)
            for e in root.iter():
                if '}' in e.tag: e.tag = e.tag.split('}', 1)[1]
            
            # 에러 체크
            if root.findtext(".//errorMsg") or root.findtext(".//message"): return False, None
            
            # 데이터 확인
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) > 0:
                if root.find(".//cargCsclPrgsInfoQryVo") is not None:
                    return True, root
            return False, None
        except:
            return False, None

    # --------------------------------
    # 입력값 분석
    # --------------------------------
    # 공백 제거 및 특수문자 제거 (순수 문자+숫자만 남김)
    raw_no = str(input_no).strip().upper()
    ref_no = re.sub(r'[^A-Z0-9]', '', raw_no) 

    # 현재 연도 기준 검색 범위 설정 (올해, 작년, 재작년, 내년)
    this_year = datetime.now().year
    years_to_check = [this_year, this_year - 1, this_year - 2, this_year + 1] # [2026, 2025, 2024, 2027]

    # 컨테이너 번호인지 확인 (ABCD1234567 형식)
    is_cntr = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))
    # 화물관리번호인지 확인 (숫자15자리 이상)
    is_mgmt = bool(re.match(r"^\d{15,}$", ref_no)) or (len(ref_no) > 15 and ref_no[:2].isdigit())

    final_root = None
    
    # --------------------------------
    # 전략 실행 (찾으면 즉시 리턴)
    # --------------------------------
    
    # [Case 1] 화물관리번호 (가장 정확)
    if is_mgmt:
        prefix = "20" + ref_no[:2] # 26... -> 2026
        success, root = call_api({"cargMtNo": ref_no, "qryYy": prefix})
        if success: final_root = root

    # [Case 2] 컨테이너 번호
    elif is_cntr:
        for yr in years_to_check:
            success, root = call_api({"cntrNo": ref_no, "qryYy": yr})
            if success: 
                final_root = root
                break

    # [Case 3] H.B/L 번호 (여기가 핵심!)
    else:
        # H.B/L은 '발행년도(blYy)'가 핵심입니다. 모든 연도를 다 찔러봅니다.
        for yr in years_to_check:
            # 1. HBL + 발행년도 (가장 표준)
            success, root = call_api({"hblNo": ref_no, "blYy": yr})
            if success: 
                final_root = root; break
            
            # 2. HBL + 입항년도 (웹사이트 방식)
            success, root = call_api({"hblNo": ref_no, "qryYy": yr})
            if success: 
                final_root = root; break
            
            # 3. 혹시 MBL 칸에 입력했나? (MBL로도 체크)
            success, root = call_api({"mblNo": ref_no, "blYy": yr})
            if success: 
                final_root = root; break

    # --------------------------------
    # 결과 파싱
    # --------------------------------
    if final_root:
        try:
            # 태그 이름 상관없이 내용으로 찾기 (Blind Search)
            nodes = []
            for e in final_root.iter():
                tags = [c.tag for c in e]
                if any(x in tags for x in ['prgsStts', 'cargTrcnNm', 'prcsDttm']):
                    nodes.append(e)
            
            if not nodes: nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")
            
            if nodes:
                # 최신순 정렬
                nodes.sort(key=lambda x: x.findtext("prcsDttm") or x.findtext("prgsDttm") or "0", reverse=True)
                latest = nodes[0]
                
                status = latest.findtext("cargTrcnNm") or latest.findtext("prgsStts")
                pdate = latest.findtext("prcsDttm") or latest.findtext("prgsDttm")
                fmt_date = f"{pdate[:4]}-{pdate[4:6]}-{pdate[6:8]}" if pdate and len(pdate) >= 8 else "-"
                
                app_st = "해상운송중"
                if status:
                    if any(x in status for x in ["반출","수리","통관","자진신고"]): app_st = "입고완료"
                    elif any(x in status for x in ["반입","하선","입항","보세","배정"]): app_st = "입항완료"
                
                return {"status": app_st, "msg": f"{status} ({fmt_date})", "delay": 0}
        except:
            pass # 파싱 에러나도 아래로 넘어감

    return {"status": "확인불가", "msg": "조회 실패 (번호확인)", "delay": 0}