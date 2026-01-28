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
    [최종 해결책] 
    데이터가 0건일 경우, '작년 데이터' 및 'M.B/L'까지 자동으로 교차 조회하여
    데이터가 나오는 조합을 찾아냅니다. (2026/2025 x HBL/MBL)
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # 1. 내부 함수: API 호출 및 데이터 존재 여부 체크
    def try_unipass_api(params):
        try:
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            response = requests.get(url, params=params, timeout=5) # 타임아웃 5초로 단축
            
            if response.status_code != 200:
                return False, None, f"서버 오류({response.status_code})"

            # XML 파싱 & 네임스페이스 제거
            try:
                root = ET.fromstring(response.content)
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
            except:
                return False, None, "XML 파싱 실패"

            # 데이터 개수(tCnt) 확인
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) > 0:
                # 상세 내역(cargCsclPrgsInfoQryVo)이 진짜 있는지 확인
                if root.find(".//cargCsclPrgsInfoQryVo") is not None:
                    return True, root, "성공"
            
            return False, root, "데이터 0건"

        except Exception as e:
            return False, None, str(e)

    # 2. 조회 준비
    ref_no = str(input_no).strip().upper()
    current_year = datetime.now().year  # 2026
    last_year = current_year - 1        # 2025
    
    is_container_format = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

    # 3. 시나리오 생성 (우선순위별로 시도)
    attempts = []

    if is_container_format:
        # 컨테이너 번호면 단순함 (올해 -> 작년)
        attempts.append({"type": "CNTR", "year": current_year, "cntrNo": ref_no})
        attempts.append({"type": "CNTR", "year": last_year, "cntrNo": ref_no})
    else:
        # B/L 번호면 4가지 경우의 수 시도
        # (1) H.B/L + 올해
        attempts.append({"type": "HBL", "year": current_year, "hblNo": ref_no})
        # (2) M.B/L + 올해
        attempts.append({"type": "MBL", "year": current_year, "mblNo": ref_no})
        # (3) H.B/L + 작년 (연초에는 작년 데이터일 확률 높음)
        attempts.append({"type": "HBL", "year": last_year, "hblNo": ref_no})
        # (4) M.B/L + 작년
        attempts.append({"type": "MBL", "year": last_year, "mblNo": ref_no})

    # 4. 순차적 실행 (데이터 나오면 즉시 중단)
    final_root = None
    last_msg = ""

    for attempt in attempts:
        # 파라미터 구성
        params = {
            "crkyCn": UNIPASS_KEY,
            "qryYy": attempt["year"],
            "cargMtNo": "", "mblNo": "", "hblNo": "", "cntrNo": "", "blYy": ""
        }
        
        if attempt["type"] == "CNTR":
            params["cntrNo"] = attempt["cntrNo"]
        else:
            if attempt["type"] == "HBL":
                params["hblNo"] = attempt["hblNo"]
            else:
                params["mblNo"] = attempt["mblNo"]
            # B/L 조회 시 blYy 필수
            params["blYy"] = attempt["year"]

        # 호출
        success, root, msg = try_unipass_api(params)
        last_msg = msg
        
        if success:
            final_root = root
            break # 찾았다! 루프 종료
    
    # 5. 결과 파싱
    if final_root is None:
        return {"status": "확인불가", "msg": "조회 결과 없음 (번호/연도 확인)", "delay": 0}

    try:
        # 상세 내역 리스트 찾기
        history_nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")
        
        # 처리일시 기준 최신순 정렬 (Blind Search)
        sorted_nodes = sorted(
            history_nodes, 
            key=lambda x: x.findtext("prcsDttm") or x.findtext("prgsDttm") or "00000000000000", 
            reverse=True
        )
        latest = sorted_nodes[0]
        
        # 상태값 추출
        raw_status = latest.findtext("cargTrcnNm") or latest.findtext("prgsStts")
        proc_date = latest.findtext("prcsDttm") or latest.findtext("prgsDttm")
        
        formatted_date = "-"
        if proc_date and len(proc_date) >= 8:
            formatted_date = f"{proc_date[:4]}-{proc_date[4:6]}-{proc_date[6:8]}"

        # 상태 매핑
        app_status = "해상운송중"
        if raw_status:
            if any(x in raw_status for x in ["반출", "수입신고수리", "통관", "자진신고", "수리"]):
                app_status = "입고완료"
            elif any(x in raw_status for x in ["반입", "하선", "입항", "보세", "배정"]):
                app_status = "입항완료"
            elif "적하목록" in raw_status:
                app_status = "해상운송중"

        return {"status": app_status, "msg": f"{raw_status} ({formatted_date})", "delay": 0}

    except Exception as e:
        return {"status": "오류", "msg": f"결과 처리 중 오류: {str(e)}", "delay": 0}