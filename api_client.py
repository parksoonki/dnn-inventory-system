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
    1. 파라미터 클리닝: 값이 없는 파라미터는 전송하지 않음 (API 오류 방지)
    2. 조회 전략 수정: 스크린샷에 맞춰 '입항년도(qryYy)' 검색을 최우선으로 시도
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # 1. 내부 함수: API 호출 (파라미터 클리닝 적용)
    def try_unipass_api(raw_params):
        try:
            # [핵심] 값이 있는 파라미터만 남기기 (빈 문자열 제거)
            clean_params = {k: v for k, v in raw_params.items() if v}
            # 인증키는 필수
            clean_params["crkyCn"] = UNIPASS_KEY

            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            response = requests.get(url, params=clean_params, timeout=5)
            
            if response.status_code != 200:
                return False, None, f"HTTP {response.status_code}"

            # XML 파싱
            try:
                root = ET.fromstring(response.content)
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
            except:
                return False, None, "XML 파싱 실패"

            # 에러 메시지 체크
            if root.findtext(".//errorMsg") or root.findtext(".//message"):
                msg = root.findtext(".//errorMsg") or root.findtext(".//message")
                return False, None, f"반려: {msg}"

            # 데이터 존재 확인
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) > 0:
                if root.find(".//cargCsclPrgsInfoQryVo") is not None:
                    return True, root, "성공"
            
            return False, root, "0건"

        except Exception as e:
            return False, None, str(e)

    # ------------------------------------
    # 2. 조회 전략 수립 (우선순위 재조정)
    # ------------------------------------
    ref_no = str(input_no).strip().upper()
    current_year = datetime.now().year  # 2026
    last_year = current_year - 1        # 2025
    
    is_container = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

    attempts = []

    if is_container:
        # 컨테이너: 입항년도(qryYy) 필수
        attempts.append({"desc": "CNTR/올해", "cntrNo": ref_no, "qryYy": current_year})
        attempts.append({"desc": "CNTR/작년", "cntrNo": ref_no, "qryYy": last_year})
    else:
        # B/L: 스크린샷에 따르면 '2026년'은 '입항년도'일 확률이 매우 높음
        
        # [1순위] H.B/L + 입항년도(qryYy) 2026 (가장 유력)
        attempts.append({"desc": "HBL/올해/입항", "hblNo": ref_no, "qryYy": current_year})
        
        # [2순위] H.B/L + 발행년도(blYy) 2026 (혹시 모르니)
        attempts.append({"desc": "HBL/올해/발행", "hblNo": ref_no, "blYy": current_year})
        
        # [3순위] H.B/L + 발행년도(blYy) 2025 (해넘이 화물)
        attempts.append({"desc": "HBL/작년/발행", "hblNo": ref_no, "blYy": last_year})
        
        # [4순위] M.B/L 시도 (혹시 HBL이 아니라면)
        attempts.append({"desc": "MBL/올해/입항", "mblNo": ref_no, "qryYy": current_year})

    final_root = None
    last_msg = ""
    
    # 3. 순차 실행
    for att in attempts:
        # 매번 새로운 파라미터 딕셔너리 생성
        params = {}
        if "cntrNo" in att: params["cntrNo"] = att["cntrNo"]
        if "hblNo" in att: params["hblNo"] = att["hblNo"]
        if "mblNo" in att: params["mblNo"] = att["mblNo"]
        if "qryYy" in att: params["qryYy"] = att["qryYy"]
        if "blYy" in att: params["blYy"] = att["blYy"]

        success, root, msg = try_unipass_api(params)
        
        if success:
            final_root = root
            break
        else:
            last_msg = f"{msg} ({att['desc']})"

    # 4. 결과 파싱 (Blind Search)
    if final_root:
        try:
            # 태그 이름에 구애받지 않고 내용으로 찾기
            history_nodes = []
            for elem in final_root.iter():
                # 자식 태그들 중 핵심 키워드가 있는지 확인
                child_tags = [child.tag for child in elem]
                if 'prgsStts' in child_tags or 'cargTrcnNm' in child_tags or 'prcsDttm' in child_tags:
                    history_nodes.append(elem)

            # 만약 못 찾았으면 정석대로 다시 검색
            if not history_nodes:
                 history_nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")

            if history_nodes:
                # 최신순 정렬 (prcsDttm or prgsDttm)
                def get_val(node, tags):
                    for t in tags:
                        found = node.findtext(t)
                        if found: return found
                    return None

                sorted_nodes = sorted(
                    history_nodes, 
                    key=lambda x: get_val(x, ["prcsDttm", "prgsDttm"]) or "0", 
                    reverse=True
                )
                latest = sorted_nodes[0]
                
                raw_status = get_val(latest, ["cargTrcnNm", "prgsStts"])
                proc_date = get_val(latest, ["prcsDttm", "prgsDttm"])
                
                fmt_date = "-"
                if proc_date and len(proc_date) >= 8:
                    fmt_date = f"{proc_date[:4]}-{proc_date[4:6]}-{proc_date[6:8]}"
                
                # 상태 매핑
                app_status = "해상운송중"
                if raw_status: 
                    if any(x in raw_status for x in ["반출","수리","통관","자진신고"]): app_status = "입고완료"
                    elif any(x in raw_status for x in ["반입","하선","입항","보세","배정"]): app_status = "입항완료"
                
                return {"status": app_status, "msg": f"{raw_status} ({fmt_date})", "delay": 0}
            else:
                 return {"status": "오류", "msg": "상세 내역 없음", "delay": 0}
        except Exception as e:
            return {"status": "오류", "msg": f"파싱 에러: {str(e)}", "delay": 0}
    else:
        return {"status": "확인불가", "msg": f"실패: {last_msg}", "delay": 0}