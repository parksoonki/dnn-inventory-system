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
    관세청 유니패스 API를 통해 화물 상태를 조회합니다.
    [수정 사항] 컨테이너 번호 형식이 아니면 House B/L로 조회하도록 로직 개선
    """
    # 1. API 키 확인
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    try:
        # 입력값 정리 (공백 제거 및 대문자)
        ref_no = str(input_no).strip().upper()
        year = datetime.now().year # 현재 연도 (필요시 작년 조회 로직 추가 가능)

        # 2. 번호 형식 체크 (컨테이너 번호: 영문 4자리 + 숫자 7자리)
        # 예: ABCD1234567 -> True / ECHWF... -> False
        is_container_format = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

        # 3. API 요청 파라미터 설정
        url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
        
        params = {
            "crkyCn": UNIPASS_KEY,
            "qryYy": year,
            "cargMtNo": "",
            "mblNo": "",
            "hblNo": "",
            "cntrNo": ""
        }

        # [핵심] 형식에 따라 파라미터 자동 선택
        if is_container_format:
            params["cntrNo"] = ref_no
        else:
            # 컨테이너 형식이 아니면 House B/L로 간주 (ECHWF... 등)
            params["hblNo"] = ref_no 

        # 4. API 호출
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code != 200:
            return {"status": "오류", "msg": f"서버 응답 오류({response.status_code})", "delay": 0}

        # 5. XML 파싱 및 결과 추출
        root = ET.fromstring(response.content)
        
        # 데이터가 아예 없는 경우
        t_cnt_tag = root.find(".//tCnt")
        if t_cnt_tag is None or int(t_cnt_tag.text) == 0:
            return {
                "status": "확인불가", 
                "msg": "데이터 없음 (번호/연도 확인)", 
                "delay": 0
            }

        # 상세 내역 리스트 조회
        history_nodes = root.findall('.//cargCsclPrgsInfoQryVo')
        
        if history_nodes:
            # 처리일시(prgsDttm) 기준 내림차순 정렬 (최신이 맨 위로)
            sorted_nodes = sorted(history_nodes, key=lambda x: x.find('prgsDttm').text if x.find('prgsDttm') is not None else "00000000000000", reverse=True)
            
            latest_node = sorted_nodes[0]
            
            # 상태명 추출 (cargTrcnNm: 화물처리단계, prgsStts: 진행상태)
            raw_status = latest_node.find('cargTrcnNm').text 
            if not raw_status:
                raw_status = latest_node.find('prgsStts').text

            # 날짜 포맷팅 (YYYYMMDDHHMMSS -> YYYY-MM-DD)
            proc_date_raw = latest_node.find('prcsDttm').text 
            formatted_date = f"{proc_date_raw[:4]}-{proc_date_raw[4:6]}-{proc_date_raw[6:8]}"

            # [상태 매핑] 화면에 보여줄 요약 상태
            app_status = "해상운송중"
            if any(x in raw_status for x in ["반출", "수입신고수리", "통관", "자진신고"]):
                app_status = "입고완료"
            elif any(x in raw_status for x in ["반입", "하선", "입항", "보세", "배정"]):
                app_status = "입항완료"
            elif "적하목록" in raw_status:
                app_status = "해상운송중"

            return {
                "status": app_status, 
                "msg": f"{raw_status} ({formatted_date})", 
                "delay": 0
            }
        else:
            return {"status": "확인불가", "msg": "상세 상태 없음", "delay": 0}

    except Exception as e:
        return {"status": "오류", "msg": f"시스템 오류: {str(e)}", "delay": 0}