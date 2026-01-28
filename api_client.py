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
    1. 태그 이름에 의존하지 않고, '진행상태(prgsStts)' 정보를 가진 노드를 직접 찾는 방식 적용
    2. B/L 조회 시 blYy(발행년도) 파라미터 적용
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # 내부 함수: API 호출 및 XML 파싱
    def call_unipass_api(params):
        try:
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code != 200:
                return None, f"서버 오류({response.status_code})"

            # XML 파싱 & 네임스페이스 제거
            try:
                root = ET.fromstring(response.content)
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
                return root, None
            except:
                return None, "XML 파싱 실패"
        except Exception as e:
            return None, str(e)

    # 1. 입력값 준비
    ref_no = str(input_no).strip().upper()
    current_year = datetime.now().year
    
    # 컨테이너 번호 형식 확인 (ABCD1234567)
    is_container_format = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

    # 2. 조회 시도 (H.B/L -> M.B/L 순서)
    root = None
    error = None

    if is_container_format:
        # 컨테이너 번호로 조회
        root, error = call_unipass_api({
            "crkyCn": UNIPASS_KEY, "cntrNo": ref_no, "qryYy": current_year,
            "cargMtNo": "", "mblNo": "", "hblNo": "", "blYy": ""
        })
    else:
        # H.B/L로 먼저 시도 (blYy 필수)
        root, error = call_unipass_api({
            "crkyCn": UNIPASS_KEY, "hblNo": ref_no, "blYy": current_year, "qryYy": current_year,
            "cargMtNo": "", "mblNo": "", "cntrNo": ""
        })
        
        # 데이터가 없으면(tCnt=0) M.B/L로 재시도
        if root is not None:
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) == 0:
                root, error = call_unipass_api({
                    "crkyCn": UNIPASS_KEY, "mblNo": ref_no, "blYy": current_year, "qryYy": current_year,
                    "cargMtNo": "", "hblNo": "", "cntrNo": ""
                })

    if error:
        return {"status": "오류", "msg": error, "delay": 0}

    # 3. 데이터 추출 (내용 기반 검색)
    try:
        # (1) 데이터 존재 여부 확인 (tCnt)
        t_cnt = root.find(".//tCnt")
        if t_cnt is None or int(t_cnt.text) == 0:
            return {"status": "확인불가", "msg": "데이터 없음 (년도/번호 확인)", "delay": 0}

        # (2) [핵심] 태그 이름 상관없이 '진행상태(prgsStts)'나 '처리일시(prcsDttm)'를 가진 노드 찾기
        history_nodes = []
        for elem in root.iter():
            # 자식 노드들의 태그 이름 목록을 만듦
            child_tags = [child.tag for child in elem]
            # 핵심 필드가 포함된 노드라면 '상세 내역'으로 간주
            if 'prgsStts' in child_tags or 'cargTrcnNm' in child_tags or 'prcsDttm' in child_tags:
                history_nodes.append(elem)

        if history_nodes:
            # 처리일시 기준 최신순 정렬
            def get_val(node, tags):
                for t in tags:
                    found = node.findtext(t)
                    if found: return found
                return None

            sorted_nodes = sorted(
                history_nodes, 
                key=lambda x: get_val(x, ["prcsDttm", "prgsDttm"]) or "00000000000000", 
                reverse=True
            )
            latest = sorted_nodes[0]
            
            # 상태명 및 날짜 추출
            raw_status = get_val(latest, ["cargTrcnNm", "prgsStts"])
            proc_date = get_val(latest, ["prcsDttm", "prgsDttm"])
            
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
        else:
            # 여전히 못 찾은 경우 (디버깅용 태그 정보 출력)
            debug_tags = list(set([e.tag for e in root.iter()]))[:5]
            return {"status": "확인불가", "msg": f"상세 내역 없음 (Tags: {debug_tags})", "delay": 0}

    except Exception as e:
        return {"status": "오류", "msg": f"파싱 에러: {str(e)}", "delay": 0}