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
    [초강력 디버깅 모드]
    관세청 API가 요구할 수 있는 모든 파라미터 조합(입항년도, 발행년도, 혼합 등)을
    순차적으로 시도하여 데이터를 찾아냅니다.
    실패 시 관세청 서버의 원본 응답 메시지를 출력하여 원인을 파악합니다.
    """
    if not UNIPASS_KEY:
        return {"status": "오류", "msg": "API 키 설정 필요", "delay": 0}

    # 내부 함수: API 호출
    def try_unipass_api(params):
        try:
            # 빈 값 제거 및 API 키 추가
            real_params = {k: v for k, v in params.items() if v}
            real_params["crkyCn"] = UNIPASS_KEY

            url = "https://unipass.customs.go.kr:38010/ext/rest/cargCsclPrgsInfoQry/retrieveCargCsclPrgsInfo"
            response = requests.get(url, params=real_params, timeout=5)
            
            if response.status_code != 200:
                return False, None, f"HTTP {response.status_code}"

            # XML 파싱
            try:
                root = ET.fromstring(response.content)
                # 네임스페이스 제거
                for elem in root.iter():
                    if '}' in elem.tag:
                        elem.tag = elem.tag.split('}', 1)[1]
            except:
                return False, None, f"XML파싱불가: {response.content[:50]}"

            # 1. 관세청 에러 메시지(errorMsg)가 있는지 확인
            err = root.findtext(".//errorMsg") or root.findtext(".//message")
            if err:
                return False, root, f"반려: {err}"

            # 2. 데이터 개수(tCnt) 확인
            t_cnt = root.find(".//tCnt")
            if t_cnt is not None and int(t_cnt.text) > 0:
                # 상세 내역 태그가 있는지 최종 확인
                if root.find(".//cargCsclPrgsInfoQryVo") is not None:
                    return True, root, "성공"
            
            return False, root, "0건"

        except Exception as e:
            return False, None, str(e)

    # ------------------------------------
    # 전략 수립: 모든 경우의 수 생성
    # ------------------------------------
    ref_no = str(input_no).strip().upper()
    cur_yr = str(datetime.now().year)   # 2026
    last_yr = str(int(cur_yr) - 1)      # 2025
    
    # 컨테이너 번호 형식인지 체크
    is_cntr = bool(re.match(r"^[A-Z]{4}\d{7}$", ref_no))

    attempts = []

    if is_cntr:
        # 컨테이너는 입항년도(qryYy)가 필수
        attempts.append({"desc": "CNTR/올해", "cntrNo": ref_no, "qryYy": cur_yr})
        attempts.append({"desc": "CNTR/작년", "cntrNo": ref_no, "qryYy": last_yr})
    else:
        # B/L은 까다로우므로 모든 조합 시도
        
        # [그룹 1] 2026년 기준 (가장 유력)
        # 1. HBL + B/L년도 (표준)
        attempts.append({"desc": "HBL/26/발행", "hblNo": ref_no, "blYy": cur_yr})
        # 2. HBL + 입항년도 (웹사이트 방식)
        attempts.append({"desc": "HBL/26/입항", "hblNo": ref_no, "qryYy": cur_yr})
        # 3. HBL + 둘 다 (강력)
        attempts.append({"desc": "HBL/26/둘다", "hblNo": ref_no, "blYy": cur_yr, "qryYy": cur_yr})
        
        # [그룹 2] 2025년 기준 (해넘이 화물)
        attempts.append({"desc": "HBL/25/발행", "hblNo": ref_no, "blYy": last_yr})
        attempts.append({"desc": "HBL/25/입항", "hblNo": ref_no, "qryYy": last_yr})
        attempts.append({"desc": "HBL/25/둘다", "hblNo": ref_no, "blYy": last_yr, "qryYy": last_yr})

        # [그룹 3] 혹시 MBL일 경우
        attempts.append({"desc": "MBL/26/발행", "mblNo": ref_no, "blYy": cur_yr})
        attempts.append({"desc": "MBL/26/입항", "mblNo": ref_no, "qryYy": cur_yr})

    # ------------------------------------
    # 순차 실행 (찾으면 즉시 중단)
    # ------------------------------------
    final_root = None
    fail_log = []

    for att in attempts:
        # 파라미터 구성
        params = {
            "cargMtNo": "", "mblNo": "", "hblNo": "", "cntrNo": "", 
            "qryYy": "", "blYy": ""
        }
        params.update({k: v for k, v in att.items() if k != "desc"})

        success, root, msg = try_unipass_api(params)
        
        if success:
            final_root = root
            break # 찾았다!
        else:
            # 실패 로그 기록 (디버깅용)
            fail_log.append(f"{att['desc']}:{msg}")

    # ------------------------------------
    # 결과 처리
    # ------------------------------------
    if final_root:
        try:
            # 내용 기반 태그 찾기 (Blind Search)
            history_nodes = []
            for elem in final_root.iter():
                tags = [c.tag for c in elem]
                if any(x in tags for x in ['prgsStts', 'cargTrcnNm', 'prcsDttm']):
                    history_nodes.append(elem)

            # 못 찾으면 정석대로
            if not history_nodes:
                history_nodes = final_root.findall(".//cargCsclPrgsInfoQryVo")

            if history_nodes:
                # 최신순 정렬
                def get_v(n, ts):
                    for t in ts:
                        v = n.findtext(t)
                        if v: return v
                    return None

                sorted_nodes = sorted(
                    history_nodes, 
                    key=lambda x: get_v(x, ["prcsDttm", "prgsDttm"]) or "0", 
                    reverse=True
                )
                latest = sorted_nodes[0]
                
                raw_st = get_v(latest, ["cargTrcnNm", "prgsStts"])
                p_date = get_v(latest, ["prcsDttm", "prgsDttm"])
                
                fmt_date = "-"
                if p_date and len(p_date) >= 8:
                    fmt_date = f"{p_date[:4]}-{p_date[4:6]}-{p_date[6:8]}"
                
                app_st = "해상운송중"
                if raw_st: 
                    if any(x in raw_st for x in ["반출","수리","통관","자진신고"]): app_st = "입고완료"
                    elif any(x in raw_st for x in ["반입","하선","입항","보세","배정"]): app_st = "입항완료"
                
                return {"status": app_st, "msg": f"{raw_st} ({fmt_date})", "delay": 0}
            else:
                 return {"status": "오류", "msg": "상세내역 없음(구조이상)", "delay": 0}
        except Exception as e:
            return {"status": "오류", "msg": f"파싱에러: {str(e)}", "delay": 0}
    else:
        # 실패 시 상세 로그 리턴 (어떤 시도가 왜 실패했는지)
        # 로그가 너무 길면 핵심만 자름
        log_str = " | ".join(fail_log)
        if len(log_str) > 50: log_str = log_str[:50] + "..."
        return {"status": "확인불가", "msg": f"실패: {log_str}", "delay": 0}