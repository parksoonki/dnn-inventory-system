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
    관세청(Unipass) 실시간 조회: 입력값이 'MBL - HBL'처럼 합쳐져 있어도 HBL/MBL/컨테이너번호를 전수 조사합니다.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 미설정", "delay": 0}

    def call_api(params):
        try:
            # 빈 값 제거 및 API 키 추가
            p = {k: v for k, v in params.items() if v}
            p["crkyCn"] = UNIPASS_KEY
            
            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            res = requests.get(url, params=p, timeout=2)
            
            if res.status_code != 200: 
                return False, None, f"HTTP 에러 {res.status_code}"
            
            # XML 파싱
            root = ET.fromstring(res.content)
            for e in root.iter(): # 네임스페이스 제거
                if '}' in e.tag: e.tag = e.tag.split('}', 1)[1]
            
            # 에러 메시지 체크 (인증키 오류 등)
            err = root.findtext(".//errorMsg") or root.findtext(".//message")
            if err: return False, None, f"관세청 에러: {err}"
            
            # 데이터 개수 체크
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) > 0:
                # 상세 내역 태그 존재하는지 확인
                if root.find(".//cargCsclPrgsInfoQryVo") is not None:
                    return True, root, "성공"
            
            return False, None, "결과 0건"
        except Exception as e:
            return False, None, f"시스템 에러: {str(e)}"

    # --------------------------------
    # 입력값 정리
    # --------------------------------
    # 입력값 정리
    # --------------------------------
    raw_no = str(input_no or "").strip().upper()

    # ✅ 핵심: 엑셀/DB에 'KMTCSHKA367040 - ECHWF26010085'처럼 두 번호가 같이 저장되면
    # 기존 방식(특수문자 제거)이 'KMTCSHKA367040ECHWF26010085'로 합쳐져 조회가 0건이 됩니다.
    # 그래서 후보 번호를 여러 개로 뽑아(HBL 우선) 전수 조사합니다.
    def build_candidates(raw: str):
        raw = str(raw or "").strip().upper()
        raw_nospace = re.sub(r"\s+", "", raw)

        candidates = []

        # 1) 컨테이너번호(ISO 6346) 우선 추출
        cm = re.search(r"[A-Z]{4}\d{7}", raw_nospace)
        if cm:
            candidates.append(cm.group(0))

        # 2) 긴 토큰 추출 (MBL/HBL 등)
        tokens = re.findall(r"[A-Z0-9]{5,}", raw)

        # 'MBL - HBL'이면 보통 HBL이 뒤에 오므로 뒤 토큰을 우선
        if "-" in raw or "–" in raw or "—" in raw:
            if len(tokens) >= 2:
                tokens = [tokens[-1], tokens[0]] + tokens[1:-1]

        for t in tokens:
            if t and t not in candidates:
                candidates.append(t)

        # 3) fallback: 전체를 특수문자 제거한 값(너무 길면 제외)
        merged = re.sub(r"[^A-Z0-9]", "", raw)
        if merged and merged not in candidates and len(merged) <= 20:
            candidates.append(merged)

        return candidates or ([merged] if merged else [])

    candidates = build_candidates(raw_no)

    # 검색할 연도 범위 (현재연도 기준 -2 ~ +1)
    this_year = datetime.now().year
    years_to_check = [this_year, this_year - 1, this_year - 2, this_year + 1]

    final_root = None
    last_error = ""

    def try_one(ref_no: str):
        """단일 번호(ref_no)에 대해 기존 로직(관리번호/컨테이너/BL)을 그대로 시도"""
        ref_no = re.sub(r'[^A-Z0-9]', '', str(ref_no or '').strip().upper())
        if not ref_no:
            return None, "빈 번호"

        # 번호 형식 체크
        is_cntr = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))
        is_mgmt = bool(re.match(r"^\d{15,}$", ref_no)) or (len(ref_no) > 15 and ref_no[:2].isdigit())

        # [Case 1] 화물관리번호 (가장 정확)
        if is_mgmt:
            prefix = "20" + ref_no[:2]
            success, root, msg = call_api({"cargMtNo": ref_no, "qryYy": prefix})
            return (root if success else None), msg

        # [Case 2] 컨테이너 번호
        if is_cntr:
            last = ""
            for yr in years_to_check:
                success, root, msg = call_api({"cntrNo": ref_no, "qryYy": str(yr)})
                if success:
                    return root, "성공"
                last = msg
            return None, last or "결과 0건"

        # [Case 3] B/L (HBL / MBL)
        last = ""
        for yr in years_to_check:
            # HBL + 발행년도(표준)
            success, root, msg = call_api({"hblNo": ref_no, "blYy": str(yr)})
            if success:
                return root, "성공"
            last = msg

            # HBL + 입항년도(웹사이트 방식)
            success, root, msg = call_api({"hblNo": ref_no, "qryYy": str(yr)})
            if success:
                return root, "성공"
            last = msg

            # HBL + 혼합(해넘이 화물용)
            success, root, msg = call_api({"hblNo": ref_no, "qryYy": str(yr), "blYy": str(yr - 1)})
            if success:
                return root, "성공"
            last = msg

            # MBL 필드 시도
            success, root, msg = call_api({"mblNo": ref_no, "blYy": str(yr)})
            if success:
                return root, "성공"
            last = msg

        return None, last or "결과 0건"

    # --------------------------------
    # 전수 조사 시작 (후보 번호 순회)
    # --------------------------------
    for cand in candidates:
        root, msg = try_one(cand)
        if root is not None:
            final_root = root
            break
        last_error = msg or last_error

    # --------------------------------
    # 결과 파싱
    # --------------------------------
    if final_root:
        try:
            # 태그 이름 상관없이 내용으로 찾기 (Blind Search)
            nodes = []
            for e in final_root.iter():
                tags = [c.tag for c in e]
                if any(x in tags for x in ['prgsStts', 'cargTrcnNm', 'prcsDttm']):
                    nodes.append(e)
            
            if not nodes: nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")
            
            if nodes:
                # 최신순 정렬
                nodes.sort(key=lambda x: x.findtext("prcsDttm") or x.findtext("prgsDttm") or "0", reverse=True)
                latest = nodes[0]
                
                status = latest.findtext("cargTrcnNm") or latest.findtext("prgsStts")
                pdate = latest.findtext("prcsDttm") or latest.findtext("prgsDttm")
                fmt_date = f"{pdate[:4]}-{pdate[4:6]}-{pdate[6:8]}" if pdate and len(pdate) >= 8 else "-"
                
                app_st = "해상운송중"
                if status:
                    if any(x in status for x in ["반출","수리","통관","자진신고"]): app_st = "입고완료"
                    elif any(x in status for x in ["반입","하선","입항","보세","배정"]): app_st = "입항완료"
                
                return {"status": app_st, "msg": f"{status} ({fmt_date})", "delay": 0}
            
            return {"status": "오류", "msg": "상세내역 없음", "delay": 0}
        except Exception as e:
            return {"status": "오류", "msg": f"파싱 에러: {str(e)}", "delay": 0}

    # 실패 시 상세 이유 리턴
    return {"status": "확인불가", "msg": f"조회 실패: {last_error}", "delay": 0}
