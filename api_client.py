import requests
import pandas as pd
import streamlit as st
import random
import time
from datetime import datetime
import xml.etree.ElementTree as ET


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
    관세청 유니패스 API를 통해 화물 상태를 조회합니다.
    [개선점] 모든 이력을 가져와서 '시간순 정렬' 후 가장 최신 상태를 반영합니다.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    try:
        year = datetime.now().year
        target_cargMtNo = None # 화물관리번호
        
        # -------------------------------------------------------
        # [Step 1] 화물관리번호(Cargo Mgmt No) 찾기
        # -------------------------------------------------------
        if any(c.isalpha() for c in input_no) and len(input_no) >= 10:
            url_step1 = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargMtNo"
            params_step1 = {
                "crkyCn": UNIPASS_KEY,
                "cntrNo": input_no,
                "blYy": year
            }
            
            try:
                res1 = requests.get(url_step1, params=params_step1, timeout=5)
                root1 = ET.fromstring(res1.text)
                
                # 최신 화물관리번호 추출
                node = root1.find('.//cargMtNoVo')
                if node is not None:
                    target_cargMtNo = node.find('cargMtNo').text
                else:
                    # 올해 데이터가 없으면 작년 데이터 조회 시도
                    params_step1["blYy"] = year - 1
                    res1_retry = requests.get(url_step1, params=params_step1, timeout=5)
                    root1_retry = ET.fromstring(res1_retry.text)
                    node_retry = root1_retry.find('.//cargMtNoVo')
                    if node_retry is not None:
                        target_cargMtNo = node_retry.find('cargMtNo').text
            except Exception:
                pass 

        # -------------------------------------------------------
        # [Step 2] 화물 진행 정보(Status) 조회
        # -------------------------------------------------------
        url_step2 = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
        params_step2 = {
            "crkyCn": UNIPASS_KEY,
            "blYy": year
        }
        
        if target_cargMtNo:
            params_step2["cargMtNo"] = target_cargMtNo
        else:
            params_step2["mblNo"] = input_no 

        response = requests.get(url_step2, params=params_step2, timeout=5)
        root = ET.fromstring(response.text)

        tCnt = root.find('.//tCnt')
        if tCnt is None or int(tCnt.text) == 0:
            return {
                "status": "확인불가", 
                "msg": "관세청 데이터 없음 (화물관리번호 매칭 실패)", 
                "delay": 0
            }

        # [핵심 수정] find -> findall로 변경하여 모든 이력 조회
        history_nodes = root.findall('.//cargCsclPrgsInfoQryVo')
        
        if history_nodes:
            # 처리일시(prgsDttm) 기준으로 내림차순 정렬 (최신이 맨 앞으로 오게)
            # prgsDttm 형식: YYYYMMDDHHMMSS
            sorted_nodes = sorted(history_nodes, key=lambda x: x.find('prgsDttm').text if x.find('prgsDttm') is not None else "00000000000000", reverse=True)
            
            latest_node = sorted_nodes[0] # 가장 최신 상태
            raw_status = latest_node.find('prgsStts').text 
            
            # [상태 매핑 표준화]
            app_status = "해상운송중"
            if "반출" in raw_status or "수입신고수리" in raw_status or "통관" in raw_status:
                app_status = "입고완료"
            elif "반입" in raw_status or "하선" in raw_status or "입항" in raw_status or "보세" in raw_status:
                app_status = "입항완료" # 
            elif "적하목록" in raw_status:
                app_status = "해상운송중"

            return {
                "status": app_status, 
                "msg": f"{raw_status} ({len(history_nodes)}건 이력 중 최신)", 
                "delay": 0
            }
        else:
            return {"status": "확인불가", "msg": "상세 상태 없음", "delay": 0}

    except Exception as e:
        return {"status": "오류", "msg": f"통신/파싱 오류: {e}", "delay": 0}