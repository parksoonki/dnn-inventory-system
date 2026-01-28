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
    [최종 해결] 유니패스 웹사이트처럼 '입항년도(qryYy)'와 'B/L년도(blYy)'를
    모두 번갈아가며 시도하여, 어떤 조건으로 등록된 화물이든 찾아냅니다.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # 내부 함수: API 호출
    def try_unipass_api(params):
        try:
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            response = requests.get(url, params=params, timeout=5)
            
            if response.status_code != 200:
                return False, None, f"HTTP {response.status_code}"

            try:
                root = ET.fromstring(response.content)
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
            except:
                return False, None, "XML 파싱 실패"

            # 에러 메시지 확인
            if root.findtext(".//errorMsg") or root.findtext(".//message"):
                return False, None, "API 에러 반환"

            # 데이터 존재 확인
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) > 0:
                # 상세 데이터(cargCsclPrgsInfoQryVo)까지 있는지 확인
                if root.find(".//cargCsclPrgsInfoQryVo") is not None:
                    return True, root, "성공"
            
            return False, root, "0건"

        except Exception as e:
            return False, None, str(e)

    # ------------------------------------
    # 전략 수립 (Brute Force)
    # ------------------------------------
    ref_no = str(input_no).strip().upper()
    current_year = datetime.now().year  # 2026
    last_year = current_year - 1        # 2025
    
    is_container = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

    # 시도할 조합 목록 (순서대로 실행)
    attempts = []

    if is_container:
        # 컨테이너는 qryYy(입항년도) 필수
        attempts.append({"desc": "CNTR/올해", "cntrNo": ref_no, "qryYy": current_year})
        attempts.append({"desc": "CNTR/작년", "cntrNo": ref_no, "qryYy": last_year})
    else:
        # B/L은 경우의 수가 많음 (웹사이트처럼 다 해봐야 함)
        
        # 1. House B/L + 입항년도 (qryYy) -> 웹사이트 기본값과 가장 유사
        attempts.append({"desc": "HBL/올해/입항년도", "hblNo": ref_no, "qryYy": current_year})
        
        # 2. House B/L + 발행년도 (blYy) -> API 문서 표준
        attempts.append({"desc": "HBL/올해/발행년도", "hblNo": ref_no, "blYy": current_year})
        
        # 3. Master B/L 시도 (올해)
        attempts.append({"desc": "MBL/올해/입항년도", "mblNo": ref_no, "qryYy": current_year})
        attempts.append({"desc": "MBL/올해/발행년도", "mblNo": ref_no, "blYy": current_year})

        # 4. 작년 데이터 시도 (연초 대비)
        attempts.append({"desc": "HBL/작년/입항년도", "hblNo": ref_no, "qryYy": last_year})
        attempts.append({"desc": "HBL/작년/발행년도", "hblNo": ref_no, "blYy": last_year})

    final_root = None
    last_msg = ""
    
    # ------------------------------------
    # 순차 실행
    # ------------------------------------
    for att in attempts:
        # 기본 파라미터 세팅
        params = {
            "crkyCn": UNIPASS_KEY,
            "cargMtNo": "", "mblNo": "", "hblNo": "", "cntrNo": "", 
            "qryYy": "", "blYy": ""
        }
        
        # 조건에 맞는 파라미터만 채움
        if "cntrNo" in att: params["cntrNo"] = att["cntrNo"]
        if "hblNo" in att: params["hblNo"] = att["hblNo"]
        if "mblNo" in att: params["mblNo"] = att["mblNo"]
        
        if "qryYy" in att: params["qryYy"] = att["qryYy"]
        if "blYy" in att: params["blYy"] = att["blYy"]

        # 호출
        success, root, msg = try_unipass_api(params)
        
        if success:
            final_root = root
            break # 찾았으면 즉시 중단
        else:
            last_msg = f"{msg} ({att['desc']})"

    # ------------------------------------
    # 결과 파싱
    # ------------------------------------
    if final_root:
        try:
            # Blind Search (내용 기반 태그 찾기)
            history_nodes = []
            for elem in final_root.iter():
                child_tags = [child.tag for child in elem]
                if 'prgsStts' in child_tags or 'cargTrcnNm' in child_tags or 'prcsDttm' in child_tags:
                    history_nodes.append(elem)

            if not history_nodes:
                 # cargCsclPrgsInfoQryVo 태그로 재시도
                 history_nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")

            if history_nodes:
                # 최신순 정렬
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