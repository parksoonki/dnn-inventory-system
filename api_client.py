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
    [최종 수정] 
    1. B/L 조회 시 'blYy'(B/L년도) 파라미터 필수 추가 (인터넷 글 반영)
    2. House B/L로 조회 실패 시 Master B/L로 자동 재시도 로직 추가
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # 내부 함수: 실제 API 호출 및 파싱 (재사용을 위해 분리)
    def call_unipass_api(params):
        try:
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code != 200:
                return None, f"서버 오류({response.status_code})"

            # XML 파싱 및 네임스페이스 제거
            root = ET.fromstring(response.content)
            for elem in root.iter():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]
            return root, None
        except Exception as e:
            return None, str(e)

    # 1. 입력값 정리
    ref_no = str(input_no).strip().upper()
    current_year = datetime.now().year # 2026
    
    # 정규식으로 컨테이너 번호 형식 확인 (ABCD1234567)
    is_container_format = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

    # -------------------------------------------------------
    # 시나리오 1: 컨테이너 번호 형식인 경우
    # -------------------------------------------------------
    if is_container_format:
        params = {
            "crkyCn": UNIPASS_KEY,
            "cntrNo": ref_no,
            "qryYy": current_year, # 컨테이너는 qryYy 사용
            "cargMtNo": "", "mblNo": "", "hblNo": "", "blYy": ""
        }
        root, error = call_unipass_api(params)

    # -------------------------------------------------------
    # 시나리오 2: B/L 번호인 경우 (ECHWF...)
    # -> H.B/L로 먼저 해보고, 안 되면 M.B/L로 재시도
    # -------------------------------------------------------
    else:
        # [시도 1] House B/L로 조회 + blYy 파라미터 추가!
        params_hbl = {
            "crkyCn": UNIPASS_KEY,
            "hblNo": ref_no,
            "blYy": current_year,  # [핵심] B/L 년도 필수
            "qryYy": current_year, # 혹시 몰라 둘 다 넣음
            "cargMtNo": "", "mblNo": "", "cntrNo": ""
        }
        root, error = call_unipass_api(params_hbl)

        # 결과 확인: 데이터가 없으면(tCnt=0) Master B/L로 재시도
        if root is not None:
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) == 0:
                # [시도 2] Master B/L로 재조회
                params_mbl = {
                    "crkyCn": UNIPASS_KEY,
                    "mblNo": ref_no, # 여기를 MBL로 변경
                    "blYy": current_year,
                    "qryYy": current_year,
                    "cargMtNo": "", "hblNo": "", "cntrNo": ""
                }
                root_retry, error_retry = call_unipass_api(params_mbl)
                if root_retry is not None:
                    # 재시도 결과 확인. 데이터 있으면 교체
                    t_cnt_retry = root_retry.find(".//tCnt")
                    if t_cnt_retry is not None and int(t_cnt_retry.text) > 0:
                        root = root_retry # 재시도 성공! 이것을 사용

    # -------------------------------------------------------
    # 결과 데이터 추출 (공통)
    # -------------------------------------------------------
    if error:
        return {"status": "오류", "msg": f"통신 에러: {error}", "delay": 0}

    try:
        # 데이터 개수 최종 확인
        t_cnt = root.find(".//tCnt")
        if t_cnt is None or int(t_cnt.text) == 0:
            return {"status": "확인불가", "msg": "데이터 없음 (B/L번호/년도 확인)", "delay": 0}

        # 상세 내역 리스트 (cargCsclPrgsInfoQryVo)
        history_nodes = root.findall(".//cargCsclPrgsInfoQryVo")
        
        if history_nodes:
            # 처리일시 기준 최신순 정렬
            sorted_nodes = sorted(
                history_nodes, 
                key=lambda x: x.findtext("prcsDttm") or x.findtext("prgsDttm") or "00000000000000", 
                reverse=True
            )
            latest = sorted_nodes[0]
            
            # 상태명 및 날짜 추출
            raw_status = latest.findtext("cargTrcnNm") or latest.findtext("prgsStts")
            proc_date = latest.findtext("prcsDttm") or latest.findtext("prgsDttm")
            
            formatted_date = "-"
            if proc_date and len(proc_date) >= 8:
                formatted_date = f"{proc_date[:4]}-{proc_date[4:6]}-{proc_date[6:8]}"

            # 앱 표시용 상태 매핑
            app_status = "해상운송중"
            if raw_status:
                if any(x in raw_status for x in ["반출", "수입신고수리", "통관", "자진신고", "수리"]):
                    app_status = "입고완료" # '수입신고수리'가 여기 포함됨
                elif any(x in raw_status for x in ["반입", "하선", "입항", "보세", "배정"]):
                    app_status = "입항완료"
                elif "적하목록" in raw_status:
                    app_status = "해상운송중"

            return {"status": app_status, "msg": f"{raw_status} ({formatted_date})", "delay": 0}
        else:
            return {"status": "확인불가", "msg": "상세 내역 비어있음", "delay": 0}

    except Exception as e:
        return {"status": "오류", "msg": f"파싱 에러: {str(e)}", "delay": 0}