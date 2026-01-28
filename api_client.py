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
    [진단 모드] 관세청 서버의 원본 응답/에러 메시지를 상세하게 표시합니다.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # 내부 함수: API 호출 및 상세 진단
    def try_unipass_api(params):
        try:
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            response = requests.get(url, params=params, timeout=5)
            
            if response.status_code != 200:
                return False, None, f"HTTP 에러({response.status_code})"

            # XML 파싱
            try:
                root = ET.fromstring(response.content)
                # 네임스페이스 제거
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
            except:
                return False, None, f"XML 파싱 실패: {response.content[:30]}..."

            # [핵심] 관세청 에러 메시지 태그 확인
            # errorMsg, message, resultMsg, nttRtなど 다양한 이름으로 에러가 올 수 있음
            err_text = None
            if root.findtext(".//errorMsg"): err_text = root.findtext(".//errorMsg")
            elif root.findtext(".//message"): err_text = root.findtext(".//message")
            elif root.findtext(".//resultMsg"): err_text = root.findtext(".//resultMsg")
            
            if err_text:
                return False, None, f"관세청 반려: {err_text}"

            # 데이터 개수 확인
            t_cnt = root.find(".//tCnt")
            if t_cnt is None:
                # tCnt가 없다는 건 정상적인 조회 응답이 아니라는 뜻
                return False, None, "응답 규격 불일치(tCnt 없음)"
            
            count = int(t_cnt.text)
            if count > 0:
                return True, root, "성공"
            
            return False, root, "데이터 0건"

        except Exception as e:
            return False, None, str(e)

    # ------------------------------------
    # 조회 실행 로직
    # ------------------------------------
    ref_no = str(input_no).strip().upper()
    current_year = datetime.now().year
    last_year = current_year - 1
    
    is_container_format = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

    # 시도할 조합 목록
    attempts = []
    if is_container_format:
        attempts.append({"type": "CNTR", "year": current_year, "no": ref_no})
        attempts.append({"type": "CNTR", "year": last_year, "no": ref_no})
    else:
        # B/L은 경우의 수를 다 해봅니다
        attempts.append({"type": "HBL", "year": current_year, "no": ref_no}) # HBL 2026
        attempts.append({"type": "MBL", "year": current_year, "no": ref_no}) # MBL 2026
        attempts.append({"type": "HBL", "year": last_year, "no": ref_no})    # HBL 2025
        attempts.append({"type": "MBL", "year": last_year, "no": ref_no})    # MBL 2025

    final_root = None
    last_fail_msg = ""
    
    for att in attempts:
        params = {
            "crkyCn": UNIPASS_KEY,
            "qryYy": att["year"],
            "cargMtNo": "", "mblNo": "", "hblNo": "", "cntrNo": "", "blYy": ""
        }
        
        if att["type"] == "CNTR": params["cntrNo"] = att["no"]
        elif att["type"] == "HBL": 
            params["hblNo"] = att["no"]
            params["blYy"] = att["year"] # B/L년도 필수
        else: 
            params["mblNo"] = att["no"]
            params["blYy"] = att["year"]

        success, root, msg = try_unipass_api(params)
        
        if success:
            final_root = root
            break
        else:
            # 실패 원인을 기록 (데이터 0건이 아니라 '관세청 반려' 에러라면 이게 중요함)
            if "관세청 반려" in msg:
                last_fail_msg = msg
            elif last_fail_msg == "": # 우선순위가 낮은 에러만 있으면 그걸로
                last_fail_msg = f"{msg} ({att['type']}/{att['year']})"

    # 결과 처리
    if final_root:
        # 성공 시 파싱 로직 (기존과 동일)
        try:
            nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")
            sorted_nodes = sorted(nodes, key=lambda x: x.findtext("prcsDttm") or "0", reverse=True)
            latest = sorted_nodes[0]
            
            raw_status = latest.findtext("cargTrcnNm") or latest.findtext("prgsStts")
            proc_date = latest.findtext("prcsDttm")
            fmt_date = f"{proc_date[:4]}-{proc_date[4:6]}-{proc_date[6:8]}" if proc_date and len(proc_date)>=8 else "-"
            
            app_status = "해상운송중"
            if raw_status and any(x in raw_status for x in ["반출","수리","통관","자진신고"]): app_status = "입고완료"
            elif raw_status and any(x in raw_status for x in ["반입","하선","입항","보세"]): app_status = "입항완료"
            
            return {"status": app_status, "msg": f"{raw_status} ({fmt_date})", "delay": 0}
        except:
            return {"status": "오류", "msg": "결과 파싱 실패", "delay": 0}
    else:
        # [핵심] 왜 실패했는지 진짜 이유를 리턴
        return {"status": "확인불가", "msg": f"실패: {last_fail_msg}", "delay": 0}