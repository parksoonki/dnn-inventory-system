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
    H.B/L 번호만으로 모든 연도와 조건을 전수 조사하여 화물을 찾습니다.
    실패 시, 단순 실패가 아니라 '왜 실패했는지(0건/인증오류)'를 리턴합니다.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 미설정", "delay": 0}

    URL = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"

    # API 호출 내부 함수 (상세 로그 리턴)
    def call_api(params):
        try:
            # 빈 값 제거 및 API 키 추가 (0 같은 값만 제거되는 것 방지)
            p = {k: v for k, v in params.items() if v is not None and str(v).strip() != ""}
            p["crkyCn"] = UNIPASS_KEY

            res = requests.get(URL, params=p, timeout=6)

            if res.status_code != 200:
                return False, None, f"HTTP 에러 {res.status_code}"

            # XML 파싱
            root = ET.fromstring(res.content)
            for e in root.iter():  # 네임스페이스 제거
                if "}" in e.tag:
                    e.tag = e.tag.split("}", 1)[1]

            # 에러 메시지 체크 (인증키 오류 등)
            err = root.findtext(".//errorMsg") or root.findtext(".//message")
            if err:
                return False, None, f"관세청 에러: {err}"

            # 데이터 개수 체크
            t_cnt_text = (root.findtext(".//tCnt") or "0").strip()
            try:
                t_cnt_val = int(t_cnt_text)
            except:
                t_cnt_val = 0

            # ✅ tCnt > 0 이면 성공으로 간주 (상세 태그명이 환경에 따라 달라도 OK)
            if t_cnt_val > 0:
                return True, root, "성공"

            return False, None, "결과 0건"

        except requests.exceptions.Timeout:
            return False, None, "HTTP 타임아웃"
        except Exception as e:
            return False, None, f"시스템 에러: {str(e)}"

    # --------------------------------
    # 입력값 정리: "MBL - HBL" 같이 들어오면 토큰 분리 후 HBL(오른쪽) 우선 조회
    # --------------------------------
    raw_no = str(input_no).strip().upper()

    toks = re.findall(r"[A-Z0-9]{5,}", raw_no)
    if toks:
        # 오른쪽 토큰(HBL일 확률 높음)을 먼저 시도
        candidates = [toks[-1]] + toks[:-1]
        # 중복 제거
        seen = set()
        candidates = [x for x in candidates if not (x in seen or seen.add(x))]
    else:
        candidates = [re.sub(r"[^A-Z0-9]", "", raw_no)]

    # 검색할 연도 범위
    this_year = datetime.now().year
    years_to_check = [this_year, this_year - 1, this_year - 2, this_year + 1]

    final_root = None
    last_error = ""

    # --------------------------------
    # 전수 조사 시작 (후보를 하나씩 시도)
    # --------------------------------
    for ref_no in candidates:
        # 번호 형식 체크
        is_cntr = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))
        is_mgmt = bool(re.match(r"^\d{15,}$", ref_no)) or (len(ref_no) > 15 and ref_no[:2].isdigit())

        # [Case 1] 화물관리번호
        if is_mgmt:
            prefix = ("20" + ref_no[:2]) if (len(ref_no) >= 2 and ref_no[:2].isdigit()) else str(this_year)
            for params in (
                {"cargMtNo": ref_no},
                {"cargMtNo": ref_no, "qryYy": prefix},
            ):
                success, root, msg = call_api(params)
                if success:
                    final_root = root
                    break
                last_error = msg
            if final_root:
                break

        # [Case 2] 컨테이너 번호
        elif is_cntr:
            for yr in years_to_check:
                for params in (
                    {"cntrNo": ref_no, "qryYy": yr},
                    {"cntrNo": ref_no, "blYy": yr},
                    {"cntrNo": ref_no},
                ):
                    success, root, msg = call_api(params)
                    if success:
                        final_root = root
                        break
                    last_error = msg
                if final_root:
                    break
            if final_root:
                break

        # [Case 3] B/L (HBL/MBL)
        else:
            for yr in years_to_check:
                tries = [
                    # HBL
                    {"hblNo": ref_no, "blYy": yr},
                    {"hblNo": ref_no, "qryYy": yr},
                    {"hblNo": ref_no, "qryYy": yr, "blYy": yr},
                    {"hblNo": ref_no, "qryYy": yr, "blYy": yr - 1},
                    {"hblNo": ref_no},  # 연도 없이도 한번
                    # MBL
                    {"mblNo": ref_no, "blYy": yr},
                    {"mblNo": ref_no, "qryYy": yr},
                    {"mblNo": ref_no, "qryYy": yr, "blYy": yr},
                    {"mblNo": ref_no},
                ]
                for params in tries:
                    success, root, msg = call_api(params)
                    if success:
                        final_root = root
                        break
                    # 에러류 메시지가 있으면 보존
                    if ("관세청 에러" in msg) or ("HTTP" in msg) or ("시스템 에러" in msg):
                        last_error = msg
                    elif not last_error:
                        last_error = msg
                if final_root:
                    break
            if final_root:
                break

    # --------------------------------
    # 결과 파싱
    # --------------------------------
    if final_root:
        try:
            nodes = []
            for e in final_root.iter():
                tags = [c.tag for c in e]
                if any(x in tags for x in ["prgsStts", "cargTrcnNm", "prcsDttm", "prgsDttm"]):
                    nodes.append(e)

            if not nodes:
                nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")

            if nodes:
                nodes.sort(
                    key=lambda x: x.findtext("prcsDttm") or x.findtext("prgsDttm") or "0",
                    reverse=True
                )
                latest = nodes[0]

                status = latest.findtext("cargTrcnNm") or latest.findtext("prgsStts") or "-"
                pdate = latest.findtext("prcsDttm") or latest.findtext("prgsDttm") or ""
                fmt_date = f"{pdate[:4]}-{pdate[4:6]}-{pdate[6:8]}" if len(pdate) >= 8 else "-"

                app_st = "해상운송중"
                if any(x in status for x in ["반출", "수리", "통관", "자진신고"]):
                    app_st = "입고완료"
                elif any(x in status for x in ["반입", "하선", "입항", "보세", "배정"]):
                    app_st = "입항완료"

                return {"status": app_st, "msg": f"{status} ({fmt_date})", "delay": 0}

            return {"status": "오류", "msg": "상세내역 없음", "delay": 0}

        except Exception as e:
            return {"status": "오류", "msg": f"파싱 에러: {str(e)}", "delay": 0}

    # 실패 시 상세 이유 리턴 (중복 '조회 실패:' 방지 위해 원문만)
    return {"status": "확인불가", "msg": (last_error or "결과 0건"), "delay": 0}