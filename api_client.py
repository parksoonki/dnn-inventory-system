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
def fetch_realtime_tracking(input_no, year_hint=None):
    """
    UNI-PASS API001 (retrieveCargCsclPrgsInfo) 조회
    - 요청 파라미터(가이드): cargMtNo / mblNo / hblNo / blYy
      * MBL/HBL 조회 시 blYy(입항년도) 필수
    """

    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 미설정", "delay": 0}

    # ----------------------------
    # API 호출
    # ----------------------------
    def call_api(params: dict):
        try:
            p = {k: v for k, v in params.items() if v is not None and str(v).strip() != ""}
            p["crkyCn"] = UNIPASS_KEY

            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            res = requests.get(url, params=p, timeout=6)

            if res.status_code != 200:
                return False, None, f"HTTP 에러 {res.status_code}"

            root = ET.fromstring(res.content)

            # 네임스페이스 제거
            for e in root.iter():
                if "}" in e.tag:
                    e.tag = e.tag.split("}", 1)[1]

            ntce = (root.findtext(".//ntceInfo") or "").strip()
            err = (root.findtext(".//errorMsg") or root.findtext(".//message") or "").strip()

            # ntceInfo가 [N00]로 시작하면 '다건' 안내일 수 있음(가이드 비고) -> 에러로 처리하지 않음
            msg_txt = err or ntce

            # 인증키/권한/시스템 에러류는 명확히 에러로 처리
            if msg_txt and not msg_txt.startswith("[N00]"):
                if any(k in msg_txt for k in ["인증", "권한", "키", "KEY", "오류", "에러", "접근", "Invalid"]):
                    return False, None, f"관세청 에러: {msg_txt}"

            # tCnt 파싱(공백/개행 안전)
            tcnt_raw = (root.findtext(".//tCnt") or "").strip()
            try:
                tcnt = int(tcnt_raw) if tcnt_raw else 0
            except:
                tcnt = 0

            if tcnt > 0:
                return True, root, msg_txt or "성공"

            return False, None, msg_txt or "결과 0건"

        except Exception as e:
            return False, None, f"시스템 에러: {e}"

    # ----------------------------
    # 입력값 -> 후보 토큰 추출
    # ----------------------------
    raw = str(input_no or "").strip().upper()
    if not raw:
        return {"status": "확인불가", "msg": "번호 미입력", "delay": 0}

    # "KMTCS... - ECHWF..." 같이 들어와도 토큰 분리해서 조회(오른쪽 토큰(HBL) 우선)
    def extract_candidates(s: str):
        toks = re.findall(r"[A-Z0-9]{5,}", (s or "").upper())
        if not toks:
            return []
        ordered = [toks[-1]] + toks[:-1]  # 보통 오른쪽이 HBL인 경우가 많음
        out = []
        for t in ordered:
            if t not in out:
                out.append(t)
        return out

    candidates = extract_candidates(raw)
    if not candidates:
        candidates = [re.sub(r"[^A-Z0-9]", "", raw)]

    # ----------------------------
    # 조회할 연도 후보(blYy)
    # ----------------------------
    this_year = datetime.now().year
    years = []

    if year_hint is not None:
        try:
            yh = int(str(year_hint)[:4])
            years.append(yh)
        except:
            pass

    for y in [this_year, this_year - 1, this_year - 2, this_year + 1]:
        if y not in years:
            years.append(y)

    final_root = None
    last_error = ""

    # ----------------------------
    # 전수 조회 로직
    # 1) cargMtNo (15~19 영숫자) 우선
    # 2) hblNo/mblNo + blYy 필수로 조회
    # ----------------------------
    for token in candidates:
        token = (token or "").strip().upper()
        if not token:
            continue

        # Case 1) 화물관리번호(cargMtNo): 15~19 영숫자 (가이드)
        if re.fullmatch(r"[A-Z0-9]{15,19}", token):
            ok, root, msg = call_api({"cargMtNo": token})
            if ok:
                final_root = root
                break
            last_error = msg or last_error

        # Case 2) HBL/MBL: blYy(입항년도) 필수
        for yr in years:
            # hbl 우선 -> mbl
            for params in (
                {"hblNo": token, "blYy": yr},
                {"mblNo": token, "blYy": yr},
            ):
                ok, root, msg = call_api(params)
                if ok:
                    final_root = root
                    break
                # 단순 0건보다, 의미 있는 메시지를 우선 저장
                if msg and msg != "결과 0건":
                    last_error = msg
                elif not last_error:
                    last_error = msg
            if final_root:
                break

        if final_root:
            break

    # ----------------------------
    # 결과 파싱
    # ----------------------------
    if not final_root:
        return {"status": "확인불가", "msg": f"조회 실패: {last_error or '결과 0건'}", "delay": 0}

    try:
        # 진행상태 노드 수집(환경별 태그 변동 대비)
        nodes = []
        for e in final_root.iter():
            child_tags = [c.tag for c in list(e)]
            has_status = ("cargTrcnNm" in child_tags) or ("prgsStts" in child_tags)
            has_time = ("prcsDttm" in child_tags) or ("prgsDttm" in child_tags)
            if has_status and has_time:
                nodes.append(e)

        if not nodes:
            nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")

        if not nodes:
            return {"status": "오류", "msg": "상세내역 없음", "delay": 0}

        def get_dt(n):
            return (n.findtext("prcsDttm") or n.findtext("prgsDttm") or "").strip()

        nodes.sort(key=get_dt, reverse=True)
        latest = nodes[0]

        status = (latest.findtext("cargTrcnNm") or latest.findtext("prgsStts") or "").strip()
        pdate = get_dt(latest)
        fmt_date = f"{pdate[:4]}-{pdate[4:6]}-{pdate[6:8]}" if len(pdate) >= 8 else "-"

        app_st = "해상운송중"
        if status:
            if any(x in status for x in ["반출", "수리", "통관", "자진신고"]):
                app_st = "입고완료"
            elif any(x in status for x in ["반입", "하선", "입항", "보세", "배정"]):
                app_st = "입항완료"

        return {"status": app_st, "msg": f"{status} ({fmt_date})", "delay": 0}

    except Exception as e:
        return {"status": "오류", "msg": f"파싱 에러: {e}", "delay": 0}