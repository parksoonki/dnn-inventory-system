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
    [진단 모드 수정판] 
    B/L 조회 시 qryYy(입항년도) 파라미터를 제거하고 blYy(발행년도)만 사용하여
    파라미터 충돌로 인한 '데이터 0건' 오류를 해결합니다.
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
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
            except:
                return False, None, f"XML 파싱 실패: {response.content[:30]}..."

            # 관세청 에러 메시지 확인
            err_text = None
            if root.findtext(".//errorMsg"): err_text = root.findtext(".//errorMsg")
            elif root.findtext(".//message"): err_text = root.findtext(".//message")
            elif root.findtext(".//resultMsg"): err_text = root.findtext(".//resultMsg")
            
            if err_text:
                return False, None, f"관세청 반려: {err_text}"

            # 데이터 개수 확인
            t_cnt = root.find(".//tCnt")
            if t_cnt is None:
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

    attempts = []
    if is_container_format:
        attempts.append({"type": "CNTR", "year": current_year, "no": ref_no})
        attempts.append({"type": "CNTR", "year": last_year, "no": ref_no})
    else:
        attempts.append({"type": "HBL", "year": current_year, "no": ref_no}) 
        attempts.append({"type": "MBL", "year": current_year, "no": ref_no}) 
        attempts.append({"type": "HBL", "year": last_year, "no": ref_no})   
        attempts.append({"type": "MBL", "year": last_year, "no": ref_no})   

    final_root = None
    last_fail_msg = ""
    
    for att in attempts:
        # [핵심 수정] 파라미터 완전 분리
        params = {
            "crkyCn": UNIPASS_KEY,
            "cargMtNo": "", "mblNo": "", "hblNo": "", "cntrNo": "", 
            "qryYy": "", "blYy": "" # 둘 다 초기화
        }
        
        if att["type"] == "CNTR": 
            params["cntrNo"] = att["no"]
            params["qryYy"] = att["year"] # 컨테이너는 qryYy 필수
        
        elif att["type"] == "HBL": 
            params["hblNo"] = att["no"]
            params["blYy"] = att["year"] # [중요] B/L은 blYy만 사용 (qryYy 제거)
            
        else: # MBL
            params["mblNo"] = att["no"]
            params["blYy"] = att["year"] # [중요] MBL도 동일

        success, root, msg = try_unipass_api(params)
        
        if success:
            final_root = root
            break
        else:
            if "관세청 반려" in msg:
                last_fail_msg = msg
            elif last_fail_msg == "":
                last_fail_msg = f"{msg} ({att['type']}/{att['year']})"

    # 결과 처리
    if final_root:
        try:
            # Blind Search (태그 이름 대신 내용으로 찾기)
            history_nodes = []
            for elem in final_root.iter():
                child_tags = [child.tag for child in elem]
                if 'prgsStts' in child_tags or 'cargTrcnNm' in child_tags or 'prcsDttm' in child_tags:
                    history_nodes.append(elem)

            if not history_nodes:
                # 상세 내역 태그를 못 찾은 경우 다시 cargCsclPrgsInfoQryVo로 시도
                history_nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")

            if history_nodes:
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
                fmt_date = f"{proc_date[:4]}-{proc_date[4:6]}-{proc_date[6:8]}" if proc_date and len(proc_date)>=8 else "-"
                
                app_status = "해상운송중"
                if raw_status: 
                    if any(x in raw_status for x in ["반출","수리","통관","자진신고"]): app_status = "입고완료"
                    elif any(x in raw_status for x in ["반입","하선","입항","보세"]): app_status = "입항완료"
                
                return {"status": app_status, "msg": f"{raw_status} ({fmt_date})", "delay": 0}
            else:
                 return {"status": "오류", "msg": "상세 내역 없음", "delay": 0}
        except:
            return {"status": "오류", "msg": "결과 파싱 실패", "delay": 0}
    else:
        return {"status": "확인불가", "msg": f"실패: {last_fail_msg}", "delay": 0}