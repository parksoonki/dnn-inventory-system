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

    #UNIPASS_KEY = st.secrets["unipass"]["api_key"]
    UNIPASS_KEY = "u240n226k041b218x020q050w0"


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
    H.B/L 번호만으로 조회.
    [상태 변경] 관세청에서 통관/반출이 완료되어도 '입고완료'가 아님.
                '통관완료'로 표시하여, 사용자가 직접 입고 처리를 하도록 함.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 미설정", "delay": 0}

    # API 호출 헬퍼
    def call_api(params):
        try:
            p = {k: v for k, v in params.items() if v}
            p["crkyCn"] = UNIPASS_KEY
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            headers = {"User-Agent": "Mozilla/5.0"}
            res = requests.get(url, params=p, headers=headers, timeout=2)
            
            if res.status_code != 200: 
                return False, None, f"HTTP {res.status_code}"
            
            root = ET.fromstring(res.content)
            for e in root.iter():
                if '}' in e.tag: e.tag = e.tag.split('}', 1)[1]
            
            err = root.findtext(".//errorMsg") or root.findtext(".//message")
            if err: return False, None, f"관세청:{err}"
            
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) > 0:
                if root.find(".//cargCsclPrgsInfoQryVo") is not None:
                    return True, root, "성공"
            return False, None, "0건"
        except Exception as e:
            return False, None, f"에러:{str(e)}"

    # 입력값 정리
    raw_no = str(input_no).strip().upper()
    ref_no = re.sub(r'[^A-Z0-9]', '', raw_no) 
    this_year = datetime.now().year
    years_to_check = [this_year, this_year - 1, this_year - 2, this_year + 1]

    is_cntr = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))
    is_mgmt = bool(re.match(r"^\d{15,}$", ref_no)) or (len(ref_no) > 15 and ref_no[:2].isdigit())

    final_root = None
    last_msg = ""
    
    # 전수 조사
    if is_mgmt:
        prefix = "20" + ref_no[:2]
        success, root, msg = call_api({"cargMtNo": ref_no, "qryYy": prefix})
        if success: final_root = root; last_msg = msg
    elif is_cntr:
        for yr in years_to_check:
            success, root, msg = call_api({"cntrNo": ref_no, "qryYy": yr})
            if success: final_root = root; break
            last_msg = msg
    else:
        for yr in years_to_check:
            for params in [
                {"hblNo": ref_no, "blYy": yr},
                {"hblNo": ref_no, "qryYy": yr},
                {"hblNo": ref_no, "qryYy": yr, "blYy": yr},
                {"hblNo": ref_no, "qryYy": yr, "blYy": yr-1},
                {"mblNo": ref_no, "blYy": yr}
            ]:
                success, root, msg = call_api(params)
                if success: final_root = root; break
                if "에러" in msg: last_msg = msg
            if final_root: break

    # 결과 파싱
    if final_root:
        try:
            nodes = []
            for e in final_root.iter():
                tags = [c.tag for c in e]
                if any(x in tags for x in ['prgsStts', 'cargTrcnNm', 'prcsDttm']):
                    nodes.append(e)
            if not nodes: nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")
            
            if nodes:
                nodes.sort(key=lambda x: x.findtext("prcsDttm") or x.findtext("prgsDttm") or "0", reverse=True)
                latest = nodes[0]
                
                status = latest.findtext("cargTrcnNm") or latest.findtext("prgsStts")
                pdate = latest.findtext("prcsDttm") or latest.findtext("prgsDttm")
                fmt_date = f"{pdate[:4]}-{pdate[4:6]}-{pdate[6:8]}" if pdate and len(pdate) >= 8 else "-"
                
                # [수정된 상태 매핑 로직]
                app_st = "해상운송중"
                if status:
                    # 1. 통관/반출 완료 -> '통관완료'로 표시 (아직 회사 입고 전)
                    if any(x in status for x in ["반출", "수입신고수리", "수입신고 수리", "자진신고수리", "자진신고 수리"]): 
                        app_st = "통관완료"
                        
                    # 2. 입항/하선 등 -> '입항완료'
                    elif any(x in status for x in ["반입", "하선", "입항", "보세", "배정", "통관", "신고"]): 
                        app_st = "입항완료"
                    
                    # 3. 적하목록
                    elif "적하목록" in status:
                        app_st = "해상운송중"
                
                return {"status": app_st, "msg": f"{status} ({fmt_date})", "delay": 0}
            
            return {"status": "오류", "msg": "상세내역 없음", "delay": 0}
        except Exception as e:
            return {"status": "오류", "msg": f"파싱 에러: {str(e)}", "delay": 0}

    return {"status": "확인불가", "msg": f"조회 실패: {last_msg}", "delay": 0}